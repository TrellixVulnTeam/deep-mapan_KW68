# Copyright 2022 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""MaskRCNN task definition."""

import os
from typing import Any, Dict, Optional, List, Tuple, Mapping

from absl import logging
import tensorflow as tf
from official.common import dataset_fn as dataset_fn_lib
from official.core import base_task
from official.core import task_factory
from official.vision.configs import maskrcnn as exp_cfg
from official.vision.dataloaders import input_reader_factory
from official.vision.dataloaders import maskrcnn_input
from official.vision.dataloaders import tf_example_decoder
from official.vision.dataloaders import tf_example_label_map_decoder
from official.vision.evaluation import coco_evaluator
from official.vision.evaluation import coco_utils
from official.vision.losses import maskrcnn_losses
from official.vision.modeling import factory


def zero_out_disallowed_class_ids(batch_class_ids: tf.Tensor,
                                  allowed_class_ids: List[int]):
  """Zero out IDs of classes not in allowed_class_ids.

  Args:
    batch_class_ids: A [batch_size, num_instances] int tensor of input
      class IDs.
    allowed_class_ids: A python list of class IDs which we want to allow.

  Returns:
      filtered_class_ids: A [batch_size, num_instances] int tensor with any
        class ID not in allowed_class_ids set to 0.
  """

  allowed_class_ids = tf.constant(allowed_class_ids,
                                  dtype=batch_class_ids.dtype)

  match_ids = (batch_class_ids[:, :, tf.newaxis] ==
               allowed_class_ids[tf.newaxis, tf.newaxis, :])

  match_ids = tf.reduce_any(match_ids, axis=2)
  return tf.where(match_ids, batch_class_ids, tf.zeros_like(batch_class_ids))


@task_factory.register_task_cls(exp_cfg.MaskRCNNTask)
class MaskRCNNTask(base_task.Task):
  """A single-replica view of training procedure.

  Mask R-CNN task provides artifacts for training/evalution procedures,
  including loading/iterating over Datasets, initializing the model, calculating
  the loss, post-processing, and customized metrics with reduction.
  """

  def build_model(self):
    """Build Mask R-CNN model."""

    input_specs = tf.keras.layers.InputSpec(
        shape=[None] + self.task_config.model.input_size)

    l2_weight_decay = self.task_config.losses.l2_weight_decay
    # Divide weight decay by 2.0 to match the implementation of tf.nn.l2_loss.
    # (https://www.tensorflow.org/api_docs/python/tf/keras/regularizers/l2)
    # (https://www.tensorflow.org/api_docs/python/tf/nn/l2_loss)
    l2_regularizer = (tf.keras.regularizers.l2(
        l2_weight_decay / 2.0) if l2_weight_decay else None)

    model = factory.build_maskrcnn(
        input_specs=input_specs,
        model_config=self.task_config.model,
        l2_regularizer=l2_regularizer)

    if self.task_config.freeze_backbone:
      model.backbone.trainable = False

    return model

  def initialize(self, model: tf.keras.Model):
    """Loading pretrained checkpoint."""

    if not self.task_config.init_checkpoint:
      return

    ckpt_dir_or_file = self.task_config.init_checkpoint
    if tf.io.gfile.isdir(ckpt_dir_or_file):
      ckpt_dir_or_file = tf.train.latest_checkpoint(ckpt_dir_or_file)

    # Restoring checkpoint.
    if self.task_config.init_checkpoint_modules == 'all':
      ckpt = tf.train.Checkpoint(**model.checkpoint_items)
      status = ckpt.read(ckpt_dir_or_file)
      status.expect_partial().assert_existing_objects_matched()
    else:
      ckpt_items = {}
      if 'backbone' in self.task_config.init_checkpoint_modules:
        ckpt_items.update(backbone=model.backbone)
      if 'decoder' in self.task_config.init_checkpoint_modules:
        ckpt_items.update(decoder=model.decoder)

      ckpt = tf.train.Checkpoint(**ckpt_items)
      status = ckpt.read(ckpt_dir_or_file)
      status.expect_partial().assert_existing_objects_matched()

    logging.info('Finished loading pretrained checkpoint from %s',
                 ckpt_dir_or_file)

  def build_inputs(
      self,
      params: exp_cfg.DataConfig,
      input_context: Optional[tf.distribute.InputContext] = None,
      dataset_fn: Optional[dataset_fn_lib.PossibleDatasetType] = None):
    """Build input dataset."""
    decoder_cfg = params.decoder.get()
    if params.decoder.type == 'simple_decoder':
      decoder = tf_example_decoder.TfExampleDecoder(
          include_mask=self._task_config.model.include_mask,
          regenerate_source_id=decoder_cfg.regenerate_source_id,
          mask_binarize_threshold=decoder_cfg.mask_binarize_threshold)
    elif params.decoder.type == 'label_map_decoder':
      decoder = tf_example_label_map_decoder.TfExampleDecoderLabelMap(
          label_map=decoder_cfg.label_map,
          include_mask=self._task_config.model.include_mask,
          regenerate_source_id=decoder_cfg.regenerate_source_id,
          mask_binarize_threshold=decoder_cfg.mask_binarize_threshold)
    else:
      raise ValueError('Unknown decoder type: {}!'.format(params.decoder.type))

    parser = maskrcnn_input.Parser(
        output_size=self.task_config.model.input_size[:2],
        min_level=self.task_config.model.min_level,
        max_level=self.task_config.model.max_level,
        num_scales=self.task_config.model.anchor.num_scales,
        aspect_ratios=self.task_config.model.anchor.aspect_ratios,
        anchor_size=self.task_config.model.anchor.anchor_size,
        dtype=params.dtype,
        rpn_match_threshold=params.parser.rpn_match_threshold,
        rpn_unmatched_threshold=params.parser.rpn_unmatched_threshold,
        rpn_batch_size_per_im=params.parser.rpn_batch_size_per_im,
        rpn_fg_fraction=params.parser.rpn_fg_fraction,
        aug_rand_hflip=params.parser.aug_rand_hflip,
        aug_scale_min=params.parser.aug_scale_min,
        aug_scale_max=params.parser.aug_scale_max,
        skip_crowd_during_training=params.parser.skip_crowd_during_training,
        max_num_instances=params.parser.max_num_instances,
        include_mask=self._task_config.model.include_mask,
        mask_crop_size=params.parser.mask_crop_size)

    if not dataset_fn:
      dataset_fn = dataset_fn_lib.pick_dataset_fn(params.file_type)

    reader = input_reader_factory.input_reader_generator(
        params,
        dataset_fn=dataset_fn,
        decoder_fn=decoder.decode,
        parser_fn=parser.parse_fn(params.is_training))
    dataset = reader.read(input_context=input_context)

    return dataset

  def _build_rpn_losses(
      self, outputs: Mapping[str, Any],
      labels: Mapping[str, Any]) -> Tuple[tf.Tensor, tf.Tensor]:
    """Build losses for Region Proposal Network (RPN)."""
    rpn_score_loss_fn = maskrcnn_losses.RpnScoreLoss(
        tf.shape(outputs['box_outputs'])[1])
    rpn_box_loss_fn = maskrcnn_losses.RpnBoxLoss(
        self.task_config.losses.rpn_huber_loss_delta)
    rpn_score_loss = tf.reduce_mean(
        rpn_score_loss_fn(outputs['rpn_scores'], labels['rpn_score_targets']))
    rpn_box_loss = tf.reduce_mean(
        rpn_box_loss_fn(outputs['rpn_boxes'], labels['rpn_box_targets']))
    return rpn_score_loss, rpn_box_loss

  def _build_frcnn_losses(
      self, outputs: Mapping[str, Any],
      labels: Mapping[str, Any]) -> Tuple[tf.Tensor, tf.Tensor]:
    """Build losses for Fast R-CNN."""
    cascade_ious = self.task_config.model.roi_sampler.cascade_iou_thresholds

    frcnn_cls_loss_fn = maskrcnn_losses.FastrcnnClassLoss()
    frcnn_box_loss_fn = maskrcnn_losses.FastrcnnBoxLoss(
        self.task_config.losses.frcnn_huber_loss_delta,
        self.task_config.model.detection_head.class_agnostic_bbox_pred)

    # Final cls/box losses are computed as an average of all detection heads.
    frcnn_cls_loss = 0.0
    frcnn_box_loss = 0.0
    num_det_heads = 1 if cascade_ious is None else 1 + len(cascade_ious)
    for cas_num in range(num_det_heads):
      frcnn_cls_loss_i = tf.reduce_mean(
          frcnn_cls_loss_fn(
              outputs['class_outputs_{}'
                      .format(cas_num) if cas_num else 'class_outputs'],
              outputs['class_targets_{}'
                      .format(cas_num) if cas_num else 'class_targets']))
      frcnn_box_loss_i = tf.reduce_mean(
          frcnn_box_loss_fn(
              outputs['box_outputs_{}'.format(cas_num
                                             ) if cas_num else 'box_outputs'],
              outputs['class_targets_{}'
                      .format(cas_num) if cas_num else 'class_targets'],
              outputs['box_targets_{}'.format(cas_num
                                             ) if cas_num else 'box_targets']))
      frcnn_cls_loss += frcnn_cls_loss_i
      frcnn_box_loss += frcnn_box_loss_i
    frcnn_cls_loss /= num_det_heads
    frcnn_box_loss /= num_det_heads
    return frcnn_cls_loss, frcnn_box_loss

  def _build_mask_loss(self, outputs: Mapping[str, Any]) -> tf.Tensor:
    """Build losses for the masks."""
    mask_loss_fn = maskrcnn_losses.MaskrcnnLoss()
    mask_class_targets = outputs['mask_class_targets']
    if self.task_config.allowed_mask_class_ids is not None:
      # Classes with ID=0 are ignored by mask_loss_fn in loss computation.
      mask_class_targets = zero_out_disallowed_class_ids(
          mask_class_targets, self.task_config.allowed_mask_class_ids)
    return tf.reduce_mean(
        mask_loss_fn(outputs['mask_outputs'], outputs['mask_targets'],
                     mask_class_targets))

  def build_losses(self,
                   outputs: Mapping[str, Any],
                   labels: Mapping[str, Any],
                   aux_losses: Optional[Any] = None) -> Dict[str, tf.Tensor]:
    """Build Mask R-CNN losses."""
    rpn_score_loss, rpn_box_loss = self._build_rpn_losses(outputs, labels)
    frcnn_cls_loss, frcnn_box_loss = self._build_frcnn_losses(outputs, labels)
    if self.task_config.model.include_mask:
      mask_loss = self._build_mask_loss(outputs)
    else:
      mask_loss = tf.constant(0.0, dtype=tf.float32)

    params = self.task_config
    model_loss = (
        params.losses.rpn_score_weight * rpn_score_loss +
        params.losses.rpn_box_weight * rpn_box_loss +
        params.losses.frcnn_class_weight * frcnn_cls_loss +
        params.losses.frcnn_box_weight * frcnn_box_loss +
        params.losses.mask_weight * mask_loss)

    total_loss = model_loss
    if aux_losses:
      reg_loss = tf.reduce_sum(aux_losses)
      total_loss = model_loss + reg_loss

    total_loss = params.losses.loss_weight * total_loss
    losses = {
        'total_loss': total_loss,
        'rpn_score_loss': rpn_score_loss,
        'rpn_box_loss': rpn_box_loss,
        'frcnn_cls_loss': frcnn_cls_loss,
        'frcnn_box_loss': frcnn_box_loss,
        'mask_loss': mask_loss,
        'model_loss': model_loss,
    }
    return losses

  def _build_coco_metrics(self):
    """Build COCO metrics evaluator."""
    if (not self._task_config.model.include_mask
       ) or self._task_config.annotation_file:
      self.coco_metric = coco_evaluator.COCOEvaluator(
          annotation_file=self._task_config.annotation_file,
          include_mask=self._task_config.model.include_mask,
          per_category_metrics=self._task_config.per_category_metrics)
    else:
      # Builds COCO-style annotation file if include_mask is True, and
      # annotation_file isn't provided.
      annotation_path = os.path.join(self._logging_dir, 'annotation.json')
      if tf.io.gfile.exists(annotation_path):
        logging.info(
            'annotation.json file exists, skipping creating the annotation'
            ' file.')
      else:
        if self._task_config.validation_data.num_examples <= 0:
          logging.info('validation_data.num_examples needs to be > 0')
        if not self._task_config.validation_data.input_path:
          logging.info('Can not create annotation file for tfds.')
        logging.info(
            'Creating coco-style annotation file: %s', annotation_path)
        coco_utils.scan_and_generator_annotation_file(
            self._task_config.validation_data.input_path,
            self._task_config.validation_data.file_type,
            self._task_config.validation_data.num_examples,
            self.task_config.model.include_mask, annotation_path,
            regenerate_source_id=self._task_config.validation_data.decoder
            .simple_decoder.regenerate_source_id)
      self.coco_metric = coco_evaluator.COCOEvaluator(
          annotation_file=annotation_path,
          include_mask=self._task_config.model.include_mask,
          per_category_metrics=self._task_config.per_category_metrics)

  def build_metrics(self, training: bool = True):
    """Build detection metrics."""
    metrics = []
    if training:
      metric_names = [
          'total_loss',
          'rpn_score_loss',
          'rpn_box_loss',
          'frcnn_cls_loss',
          'frcnn_box_loss',
          'mask_loss',
          'model_loss'
      ]
      for name in metric_names:
        metrics.append(tf.keras.metrics.Mean(name, dtype=tf.float32))

    else:
      if self._task_config.use_coco_metrics:
        self._build_coco_metrics()
      if self._task_config.use_wod_metrics:
        # To use Waymo open dataset metrics, please install one of the pip
        # package `waymo-open-dataset-tf-*` from
        # https://github.com/waymo-research/waymo-open-dataset/blob/master/docs/quick_start.md#use-pre-compiled-pippip3-packages-for-linux
        # Note that the package is built with specific tensorflow version and
        # will produce error if it does not match the tf version that is
        # currently used.
        try:
          from official.vision.evaluation import wod_detection_evaluator  # pylint: disable=g-import-not-at-top
        except ModuleNotFoundError:
          logging.error('waymo-open-dataset should be installed to enable Waymo'
                        ' evaluator.')
          raise
        self.wod_metric = wod_detection_evaluator.WOD2dDetectionEvaluator()

    return metrics

  def train_step(self,
                 inputs: Tuple[Any, Any],
                 model: tf.keras.Model,
                 optimizer: tf.keras.optimizers.Optimizer,
                 metrics: Optional[List[Any]] = None):
    """Does forward and backward.

    Args:
      inputs: a dictionary of input tensors.
      model: the model, forward pass definition.
      optimizer: the optimizer for this training step.
      metrics: a nested structure of metrics objects.

    Returns:
      A dictionary of logs.
    """
    images, labels = inputs
    num_replicas = tf.distribute.get_strategy().num_replicas_in_sync
    with tf.GradientTape() as tape:
      outputs = model(
          images,
          image_shape=labels['image_info'][:, 1, :],
          anchor_boxes=labels['anchor_boxes'],
          gt_boxes=labels['gt_boxes'],
          gt_classes=labels['gt_classes'],
          gt_masks=(labels['gt_masks'] if self.task_config.model.include_mask
                    else None),
          training=True)
      outputs = tf.nest.map_structure(
          lambda x: tf.cast(x, tf.float32), outputs)

      # Computes per-replica loss.
      losses = self.build_losses(
          outputs=outputs, labels=labels, aux_losses=model.losses)
      scaled_loss = losses['total_loss'] / num_replicas

      # For mixed_precision policy, when LossScaleOptimizer is used, loss is
      # scaled for numerical stability.
      if isinstance(optimizer, tf.keras.mixed_precision.LossScaleOptimizer):
        scaled_loss = optimizer.get_scaled_loss(scaled_loss)

    tvars = model.trainable_variables
    grads = tape.gradient(scaled_loss, tvars)
    # Scales back gradient when LossScaleOptimizer is used.
    if isinstance(optimizer, tf.keras.mixed_precision.LossScaleOptimizer):
      grads = optimizer.get_unscaled_gradients(grads)
    optimizer.apply_gradients(list(zip(grads, tvars)))

    logs = {self.loss: losses['total_loss']}

    if metrics:
      for m in metrics:
        m.update_state(losses[m.name])

    return logs

  def validation_step(self,
                      inputs: Tuple[Any, Any],
                      model: tf.keras.Model,
                      metrics: Optional[List[Any]] = None):
    """Validatation step.

    Args:
      inputs: a dictionary of input tensors.
      model: the keras.Model.
      metrics: a nested structure of metrics objects.

    Returns:
      A dictionary of logs.
    """
    images, labels = inputs

    outputs = model(
        images,
        anchor_boxes=labels['anchor_boxes'],
        image_shape=labels['image_info'][:, 1, :],
        training=False)

    logs = {self.loss: 0}
    if self._task_config.use_coco_metrics:
      coco_model_outputs = {
          'detection_boxes': outputs['detection_boxes'],
          'detection_scores': outputs['detection_scores'],
          'detection_classes': outputs['detection_classes'],
          'num_detections': outputs['num_detections'],
          'source_id': labels['groundtruths']['source_id'],
          'image_info': labels['image_info']
      }
      if self.task_config.model.include_mask:
        coco_model_outputs.update({
            'detection_masks': outputs['detection_masks'],
        })
      logs.update(
          {self.coco_metric.name: (labels['groundtruths'], coco_model_outputs)})

    if self.task_config.use_wod_metrics:
      wod_model_outputs = {
          'detection_boxes': outputs['detection_boxes'],
          'detection_scores': outputs['detection_scores'],
          'detection_classes': outputs['detection_classes'],
          'num_detections': outputs['num_detections'],
          'source_id': labels['groundtruths']['source_id'],
          'image_info': labels['image_info']
      }
      logs.update(
          {self.wod_metric.name: (labels['groundtruths'], wod_model_outputs)})
    return logs

  def aggregate_logs(self, state=None, step_outputs=None):
    if self._task_config.use_coco_metrics:
      if state is None:
        self.coco_metric.reset_states()
      self.coco_metric.update_state(
          step_outputs[self.coco_metric.name][0],
          step_outputs[self.coco_metric.name][1])
    if self._task_config.use_wod_metrics:
      if state is None:
        self.wod_metric.reset_states()
      self.wod_metric.update_state(
          step_outputs[self.wod_metric.name][0],
          step_outputs[self.wod_metric.name][1])
    if state is None:
      # Create an arbitrary state to indicate it's not the first step in the
      # following calls to this function.
      state = True
    return state

  def reduce_aggregated_logs(self, aggregated_logs, global_step=None):
    logs = {}
    if self._task_config.use_coco_metrics:
      logs.update(self.coco_metric.result())
    if self._task_config.use_wod_metrics:
      logs.update(self.wod_metric.result())
    return logs
