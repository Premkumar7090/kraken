# -*- coding: utf-8 -*-
#
# Copyright 2015 Benjamin Kiessling
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing
# permissions and limitations under the License.
"""
Training loop interception helpers
"""
import os
import re
import abc
import sys
import torch
import pathlib
import logging
import numpy as np
import torch.nn.functional as F
import pytorch_lightning as pl

from itertools import cycle
from functools import partial
from tqdm import tqdm as _tqdm
from torch.multiprocessing import Pool
from typing import cast, Callable, Dict, Optional, Sequence, Union, Any, List
from pytorch_lightning.callbacks import EarlyStopping, Callback
from pytorch_lightning.callbacks.progress.tqdm_progress import Tqdm

from kraken.lib import models, vgsl, segmentation, default_specs
from kraken.lib.xml import preparse_xml_data
from kraken.lib.util import make_printable
from kraken.lib.codec import PytorchCodec
from kraken.lib.dataset import (ArrowIPCRecognitionDataset, BaselineSet,
                                GroundTruthDataset, PolygonGTDataset,
                                ImageInputTransforms, InfiniteDataLoader,
                                compute_error, collate_sequences)
from kraken.lib.models import validate_hyper_parameters
from kraken.lib.exceptions import KrakenInputException, KrakenEncodeException

from torch.utils.data import DataLoader, random_split, Subset


logger = logging.getLogger(__name__)


def _star_fun(fun, kwargs):
    try:
        return fun(**kwargs)
    except FileNotFoundError as e:
        logger.warning(f'{e.strerror}: {e.filename}. Skipping.')
    except KrakenInputException as e:
        logger.warning(str(e))
    return None


class KrakenTrainer(pl.Trainer):
    def __init__(self,
                 callbacks: Optional[Union[List[Callback], Callback]] = None,
                 enable_progress_bar: bool = True,
                 min_epochs=5,
                 max_epochs=100,
                 *args,
                 **kwargs):
        kwargs['logger'] = False
        kwargs['enable_checkpointing'] = False
        kwargs['enable_progress_bar'] = enable_progress_bar
        kwargs['min_epochs'] = min_epochs
        kwargs['max_epochs'] = max_epochs
        if enable_progress_bar:
            progress_bar_cb = KrakenProgressBar()
            if 'callbacks' in kwargs:
                if isinstance(kwargs['callbacks'], list):
                    kwargs['callbacks'].append(progress_bar_cb)
                else:
                    kwargs['callbacks'] = list(kwargs['callbacks'], progress_bar_cb)
            else:
                kwargs['callbacks'] = progress_bar_cb
        super().__init__(*args, **kwargs)

    def on_validation_end(self):
        if not self.sanity_checking:
            # fill one_channel_mode after 1 iteration over training data set
            if self.current_epoch == 0 and self.model.nn.model_type == 'recognition':
                im_mode = self.model.train_set.dataset.im_mode
                if im_mode in ['1', 'L']:
                    logger.info(f'Setting model one_channel_mode to {im_mode}.')
                    self.model.nn.one_channel_mode = im_mode
            self.model.nn.hyper_params['completed_epochs'] += 1
            self.model.nn.user_metadata['accuracy'].append(((self.current_epoch+1)*len(self.model.train_set),
                                                             {k: float(v) for k, v in  self.logged_metrics.items()}))

            logger.info('Saving to {}_{}'.format(self.model.output, self.current_epoch))
            self.model.nn.save_model(f'{self.model.output}_{self.current_epoch}.mlmodel')


class KrakenProgressBar(pl.callbacks.TQDMProgressBar):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bar_format = '{l_bar}{bar}|{n_fmt}/{total_fmt} [{elapsed}<{remaining}{postfix}]'

    def on_validation_start(self, trainer, pl_module):
        if not trainer.sanity_checking:
            print()
        super().on_validation_epoch_start(trainer, pl_module)

    def on_train_epoch_start(self, trainer, pl_module):
        print()
        super().on_train_epoch_start(trainer, pl_module)

    def on_train_epoch_start(self, trainer, pl_module):
        super().on_train_epoch_start(trainer, pl_module)
        self.main_progress_bar.reset(total=self.total_train_batches)
        self.main_progress_bar.n = self.train_batch_idx
        self.main_progress_bar.set_description(f"stage {trainer.current_epoch}/{trainer.max_epochs if pl_module.hparams.quit == 'dumb' else '∞'}")

    def get_metrics(self, trainer, model):
        # don't show the version number
        items = super().get_metrics(trainer, model)
        items.pop('v_num', None)
        items.pop('loss', None)
        items.pop('val_metric', None)
        return items

    def init_train_tqdm(self):
        bar = Tqdm(desc="Training",
                   initial=self.train_batch_idx,
                   disable=self.is_disabled,
                   position=(2 * self.process_position),
                   leave=True,
                   dynamic_ncols=True,
                   file=sys.stdout,
                   smoothing=0,
                   bar_format=self.bar_format)
        return bar

    def init_validation_tqdm(self):
        # The main progress bar doesn't exist in `trainer.validate()`
        bar = Tqdm(desc="validating",
                   disable=self.is_disabled,
                   position=(2 * self.process_position),
                   leave=True,
                   dynamic_ncols=True,
                   file=sys.stdout,
                   bar_format=self.bar_format)
        return bar

    def init_sanity_tqdm(self):
        bar = Tqdm(desc="validation sanity check",
                   disable=self.is_disabled,
                   position=(2 * self.process_position),
                   leave=True,
                   dynamic_ncols=True,
                   file=sys.stdout,
                   bar_format=self.bar_format)
        return bar


class RecognitionModel(pl.LightningModule):
    def __init__(self,
                 hyper_params: Dict[str, Any] = None,
                 output: str = 'model',
                 spec: str = default_specs.RECOGNITION_SPEC,
                 append: Optional[int] = None,
                 model: Optional[Union[pathlib.Path, str]] = None,
                 reorder: Union[bool, str] = True,
                 training_data: Union[Sequence[Union[pathlib.Path, str]], Sequence[Dict[str, Any]]] = None,
                 evaluation_data: Optional[Union[Sequence[Union[pathlib.Path, str]], Sequence[Dict[str, Any]]]] = None,
                 partition: Optional[float] = 0.9,
                 num_workers: int = 1,
                 load_hyper_parameters: bool = False,
                 repolygonize: bool = False,
                 force_binarization: bool = False,
                 format_type: str = 'path',
                 codec: Optional[Dict] = None,
                 resize: str = 'fail',
                 augment: bool = False):
        """
        A LightningModule encapsulating the training setup for a text
        recognition model.

        Setup parameters (load, training_data, evaluation_data, ....) are
        named, model hyperparameters (everything in
        `kraken.lib.default_specs.RECOGNITION_HYPER_PARAMS`) are in in the
        `hyper_params` argument.

        Args:
            hyper_params (dict): Hyperparameter dictionary containing all fields
                                 from
                                 kraken.lib.default_specs.RECOGNITION_HYPER_PARAMS
            **kwargs: Setup parameters, i.e. CLI parameters of the train() command.
        """
        super().__init__()
        hyper_params_ = default_specs.RECOGNITION_HYPER_PARAMS
        if model:
            logger.info(f'Loading existing model from {model} ')
            self.nn = vgsl.TorchVGSLModel.load_model(model)
            if load_hyper_parameters:
                hp = self.nn.hyper_params
            else:
                hp = {}
            hyper_params_.update(hp)
        else:
            self.nn = None

        if hyper_params:
            hyper_params_.update(hyper_params)
        self.save_hyperparameters(hyper_params_)

        self.reorder = reorder
        self.append = append
        self.model = model
        self.num_workers = num_workers
        self.resize = resize
        self.format_type = format_type
        self.output = output

        self.best_epoch = 0
        self.best_metric = 0.0

        DatasetClass = GroundTruthDataset
        valid_norm = True
        if format_type in ['xml', 'page', 'alto']:
            logger.info(f'Parsing {len(training_data)} XML files for training data')
            training_data = preparse_xml_data(training_data, format_type, repolygonize)
            if evaluation_data:
                logger.info(f'Parsing {len(evaluation_data)} XML files for validation data')
                evaluation_data = preparse_xml_data(evaluation_data, format_type, repolygonize)
            DatasetClass = PolygonGTDataset
            valid_norm = False
        elif format_type == 'binary':
            DatasetClass = ArrowIPCRecognitionDataset
            if repolygonize:
                logger.warning('Repolygonization enabled in `binary` mode. Will be ignored.')
            valid_norm = False
            logger.info(f'Got {len(training_data)} binary dataset files for training data')
            training_data = [{'file': file} for file in training_data]
            if evaluation_data:
                logger.info(f'Got {len(evaluation_data)} binary dataset files for validation data')
                evaluation_data = [{'file': file} for file in evaluation_data]
        elif format_type == 'path':
            if force_binarization:
                logger.warning('Forced binarization enabled in `path` mode. Will be ignored.')
                force_binarization = False
            if repolygonize:
                logger.warning('Repolygonization enabled in `path` mode. Will be ignored.')
            logger.info(f'Got {len(training_data)} line strip images for training data')
            training_data = [{'image': im} for im in training_data]
            if evaluation_data:
                logger.info(f'Got {len(evaluation_data)} line strip images for validation data')
                evaluation_data = [{'image': im} for im in evaluation_data]
            valid_norm = True
        # format_type is None. Determine training type from length of training data entry
        elif not format_type:
            if len(training_data[0]) >= 4:
                DatasetClass = PolygonGTDataset
                valid_norm = False
            else:
                if force_binarization:
                    logger.warning('Forced binarization enabled with box lines. Will be ignored.')
                    force_binarization = False
                if repolygonize:
                    logger.warning('Repolygonization enabled with box lines. Will be ignored.')
        else:
            raise ValueError(f'format_type {format_type} not in [alto, page, xml, path, binary].')

        # preparse input sizes from vgsl string to seed ground truth data set
        # sizes and dimension ordering.
        if not self.nn:
            spec = spec.strip()
            if spec[0] != '[' or spec[-1] != ']':
                raise ValueError(f'VGSL spec {spec} not bracketed')
            blocks = spec[1:-1].split(' ')
            m = re.match(r'(\d+),(\d+),(\d+),(\d+)', blocks[0])
            if not m:
                raise ValueError(f'Invalid input spec {blocks[0]}')
            batch, height, width, channels = [int(x) for x in m.groups()]
            self.spec = spec
        else:
            batch, channels, height, width = self.nn.input

        self.transforms = ImageInputTransforms(batch,
                                               height,
                                               width,
                                               channels,
                                               self.hparams.pad,
                                               valid_norm,
                                               force_binarization)

        if 'file_system' in torch.multiprocessing.get_all_sharing_strategies():
            logger.debug('Setting multiprocessing tensor sharing strategy to file_system')
            torch.multiprocessing.set_sharing_strategy('file_system')

        train_set = self._build_dataset(DatasetClass, training_data)
        if evaluation_data:
            self.train_set = Subset(train_set, range(len(train_set)))
            val_set = self._build_dataset(DatasetClass, evaluation_data)
            self.val_set = Subset(val_set, range(len(val_set)))
        else:
            train_len = int(len(train_set)*partition)
            val_len = len(train_set) - train_len
            logger.info(f'No explicit validation data provided. Splitting off '
                        f'{val_len} (of {len(train_set)}) samples to validation '
                         'set. (Will disable alphabet mismatch detection.)')
            self.train_set, self.val_set = random_split(train_set, (train_len, val_len))

        if len(self.train_set) == 0 or len(self.val_set) == 0:
            raise ValueError('No valid training data was provided to the train '
                             'command. Please add valid XML, line, or binary data.')

        logger.info(f'Training set {len(self.train_set)} lines, validation set '
                    f'{len(self.val_set)} lines, alphabet {len(train_set.alphabet)} '
                    'symbols')
        alpha_diff_only_train = set(self.train_set.dataset.alphabet).difference(set(self.val_set.dataset.alphabet))
        alpha_diff_only_val = set(self.val_set.dataset.alphabet).difference(set(self.train_set.dataset.alphabet))
        if alpha_diff_only_train:
            logger.warning(f'alphabet mismatch: chars in training set only: '
                           f'{alpha_diff_only_train} (not included in accuracy test '
                           'during training)')
        if alpha_diff_only_val:
            logger.warning(f'alphabet mismatch: chars in validation set only: {alpha_diff_only_val} (not trained)')
        logger.info('grapheme\tcount')
        for k, v in sorted(train_set.alphabet.items(), key=lambda x: x[1], reverse=True):
            char = make_printable(k)
            if char == k:
                char = '\t' + char
            logger.info(f'{char}\t{v}')

        if codec:
            logger.info('Instantiating codec')
            self.codec = PytorchCodec(codec)
            for k, v in codec.c2l.items():
                char = make_printable(k)
                if char == k:
                    char = '\t' + char
                logger.info(f'{char}\t{v}')
        else:
            self.codec = None

        logger.info('Encoding training set')

    def _build_dataset(self,
                       DatasetClass,
                       training_data):
        dataset = DatasetClass(normalization=self.hparams.normalization,
                               whitespace_normalization=self.hparams.normalize_whitespace,
                               reorder=self.reorder,
                               im_transforms=self.transforms,
                               augmentation=self.hparams.augment)

        if (self.num_workers and self.num_workers > 1) and self.format_type != 'binary':
            with Pool(processes=self.num_workers) as pool:
                for im in pool.imap_unordered(partial(_star_fun, dataset.parse), training_data, 5):
                    logger.debug(f'Adding sample {im} to training set')
                    if im:
                        dataset.add(**im)
        else:
            for im in training_data:
                try:
                    dataset.add(**im)
                except KrakenInputException as e:
                    logger.warning(str(e))
        return dataset

    def forward(self, x, seq_lens):
        return self.net(x, seq_lens)

    def training_step(self, batch, batch_idx):
        input, target = batch['image'], batch['target']
        # sequence batch
        if 'seq_lens' in batch:
            seq_lens, label_lens = batch['seq_lens'], batch['target_lens']
            target = (target, label_lens)
            o = self.net(input, seq_lens)
        else:
            o = self.net(input)

        seq_lens = o[1]
        output = o[0]
        target_lens = target[1]
        target = target[0]
        # height should be 1 by now
        if output.size(2) != 1:
            raise KrakenInputException('Expected dimension 3 to be 1, actual {}'.format(output.size(2)))
        output = output.squeeze(2)
        # NCW -> WNC
        loss = self.nn.criterion(output.permute(2, 0, 1),  # type: ignore
                                 target,
                                 seq_lens,
                                 target_lens)
        return loss

    def validation_step(self, batch, batch_idx):
        chars, error = compute_error(self.rec_nn, batch)
        chars = torch.tensor(chars)
        error = torch.tensor(error)
        return {'chars': chars, 'error': error}

    def validation_epoch_end(self, outputs):
        chars = torch.stack([x['chars'] for x in outputs]).sum()
        error = torch.stack([x['error'] for x in outputs]).sum()
        accuracy = (chars - error) / (chars + torch.finfo(torch.float).eps)
        if accuracy > self.best_metric:
            self.best_epoch = self.current_epoch
            self.best_metric = accuracy
        self.log_dict({'val_accuracy': accuracy, 'val_metric': accuracy}, prog_bar=True)

    def configure_optimizers(self):
        logger.debug(f'Constructing {self.hparams.optimizer} optimizer (lr: {self.hparams.lrate}, momentum: {self.hparams.momentum})')
        if self.hparams.optimizer == 'Adam':
            optim = torch.optim.Adam(self.nn.nn.parameters(), lr=self.hparams.lrate, weight_decay=self.hparams.weight_decay)
        else:
            optim = getattr(torch.optim, self.hparams.optimizer)(self.nn.nn.parameters(),
                                                                 lr=self.hparams.lrate,
                                                                 momentum=self.hparams.momentum,
                                                                 weight_decay=self.hparams.weight_decay)
        return {'optimizer': optim,}

    def setup(self, stage: Optional[str] = None):
        # finalize models in case of appending/loading
        if stage in [None, 'fit']:
            if self.append:
                self.train_set.dataset.encode(self.codec)
                # now we can create a new model
                spec = '[{} O1c{}]'.format(spec[1:-1], self.train_set.dataset.codec.max_label + 1)
                logger.info(f'Appending {self.spec} to existing model {self.nn.spec} after {self.append}')
                self.nn.append(self.append, spec)
                self.nn.add_codec(self.train_set.dataset.codec)
                logger.info(f'Assembled model spec: {self.nn.spec}')
            elif self.model:
                # prefer explicitly given codec over network codec if mode is 'both'
                codec = self.codec if (self.codec and self.resize == 'both') else self.nn.codec

                codec.strict = True

                try:
                    self.train_set.dataset.encode(codec)
                except KrakenEncodeException:
                    alpha_diff = set(self.train_set.dataset.alphabet).difference(set(codec.c2l.keys()))
                    if self.resize == 'fail':
                        raise KrakenInputException(f'Training data and model codec alphabets mismatch: {alpha_diff}')
                    elif self.resize == 'add':
                        logger.info(f'Resizing codec to include {len(alpha_diff)} new code points')
                        codec = codec.add_labels(alpha_diff)
                        self.nn.add_codec(codec)
                        logger.info(f'Resizing last layer in network to {codec.max_label+1} outputs')
                        self.nn.resize_output(codec.max_label + 1)
                        self.train_set.dataset.encode(nn.codec)
                    elif self.resize == 'both':
                        logger.info(f'Resizing network or given codec to {self.train_set.dataset.alphabet} code sequences')
                        self.train_set.dataset.encode(None)
                        ncodec, del_labels = codec.merge(self.train_set.dataset.codec)
                        self.nn.add_codec(ncodec)
                        logger.info(f'Deleting {len(del_labels)} output classes from network '
                                    f'({len(codec)-len(del_labels)} retained)')
                        self.train_set.dataset.encode(ncodec)
                        self.nn.resize_output(ncodec.max_label + 1, del_labels)
                    else:
                        raise ValueError(f'invalid resize parameter value {self.resize}')
            else:
                self.train_set.dataset.encode(self.codec)
                logger.info(f'Creating new model {self.spec} with {self.train_set.dataset.codec.max_label+1} outputs')
                self.spec = '[{} O1c{}]'.format(self.spec[1:-1], self.train_set.dataset.codec.max_label + 1)
                self.nn = vgsl.TorchVGSLModel(self.spec)
                # initialize weights
                self.nn.init_weights()
                self.nn.add_codec(self.train_set.dataset.codec)

            self.val_set.dataset.encode(self.nn.codec)

            if self.nn.one_channel_mode and self.train_set.dataset.im_mode != self.nn.one_channel_mode:
                logger.warning(f'Neural network has been trained on mode {nn.one_channel_mode} images, '
                               f'training set contains mode {self.train_set.dataset.im_mode} data. Consider setting `force_binarization`')

            if self.format_type != 'path' and self.nn.seg_type == 'bbox':
                logger.warning('Neural network has been trained on bounding box image information but training set is polygonal.')

            self.nn.hyper_params = self.hparams
            self.nn.model_type = 'recognition'

            if 'seg_type' not in self.nn.user_metadata:
                self.nn.user_metadata['seg_type'] = self.train_set.dataset.seg_type

            self.rec_nn = models.TorchSeqRecognizer(self.nn, train=None, device=None)
            self.net = self.nn.nn

    def train_dataloader(self):
        return DataLoader(self.train_set,
                          batch_size=self.hparams.batch_size,
                          num_workers=self.num_workers,
                          pin_memory=True,
                          collate_fn=collate_sequences)

    def val_dataloader(self):
        return DataLoader(self.val_set,
                          shuffle=False,
                          batch_size=self.hparams.batch_size,
                          num_workers=self.num_workers,
                          pin_memory=True,
                          collate_fn=collate_sequences)

    def configure_callbacks(self):
        callbacks = []
        if self.hparams.quit == 'early':
            callbacks.append(EarlyStopping(monitor='val_accuracy',
                                           mode='max',
                                           verbose=True,
                                           patience=self.hparams.lag,
                                           stopping_threshold=1.0))
        return callbacks


class SegmentationModel(pl.LightningModule):
    def __init__(self, model: vgsl.TorchVGSLModel, hyper_params):
        super().__init__()
        self.nn = model
        self.save_hyperparameters()

    def forward(self, x):
        return self.nn(x)

    def training_step(self, batch, batch_idx):
        input, target = batch['image'], batch['target']
        output, _ = self.model.nn(input)
        output = F.interpolate(output, size=(target.size(2), target.size(3)))
        loss = criterion(output, target)
        return loss

    def validation_step(self, batch, batch_idx):
        smooth = torch.finfo(torch.float).eps
        val_set = val_loader.dataset
        corrects = torch.zeros(val_set.num_classes, dtype=torch.double).to(device)
        all_n = torch.zeros(val_set.num_classes, dtype=torch.double).to(device)
        intersections = torch.zeros(val_set.num_classes, dtype=torch.double).to(device)
        unions = torch.zeros(val_set.num_classes, dtype=torch.double).to(device)
        cls_cnt = torch.zeros(val_set.num_classes, dtype=torch.double).to(device)
        model.eval()

        with torch.no_grad():
            for batch in val_loader:
                x, y = batch['image'], batch['target']
                x = x.to(device)
                y = y.to(device)
                pred, _ = model.nn(x)
                # scale target to output size
                y = F.interpolate(y, size=(pred.size(2), pred.size(3))).squeeze(0).bool()
                pred = segmentation.denoising_hysteresis_thresh(pred.detach().squeeze().cpu().numpy(), 0.2, 0.3, 0)
                pred = torch.from_numpy(pred.astype('bool')).to(device)
                pred = pred.view(pred.size(0), -1)
                y = y.view(y.size(0), -1)
                intersections += (y & pred).sum(dim=1, dtype=torch.double)
                unions += (y | pred).sum(dim=1, dtype=torch.double)
                corrects += torch.eq(y, pred).sum(dim=1, dtype=torch.double)
                cls_cnt += y.sum(dim=1, dtype=torch.double)
                all_n += y.size(1)
        model.train()
        # all_positives = tp + fp
        # actual_positives = tp + fn
        # true_positivies = tp
        pixel_accuracy = corrects.sum() / all_n.sum()
        mean_accuracy = torch.mean(corrects / all_n)
        iu = (intersections + smooth) / (unions + smooth)
        mean_iu = torch.mean(iu)
        freq_iu = torch.sum(cls_cnt / cls_cnt.sum() * iu)
        return {'accuracy': pixel_accuracy,
                'mean_acc': mean_accuracy,
                'mean_iu': mean_iu,
                'freq_iu': freq_iu,
                'val_metric': mean_iu}

    def validation_epoch_end(self, validation_step_outputs):
        pass

    def test_step(self, batch, batch_idx):
        pass

    def configure_optimizers(self):
        if self.hparams.optimizer == 'Adam':
            optim = torch.optim.Adam(self.nn.nn.parameters(), lr=self.hparams.lrate, weight_decay=self.hparams.weight_decay)
        else:
            optim = getattr(torch.optim, self.hparams.optimizer)(self.nn.nn.parameters(),
                                                                 lr=self.hparams.lrate,
                                                                 momentum=self.hparams.momentum,
                                                                 weight_decay=self.hparams.weight_decay)


#class KrakenTrainer(object):
#    """
#    Class encapsulating the recognition model training process.
#    """
#
#    def __init__(self,
#                 model: vgsl.TorchVGSLModel,
#                 optimizer: torch.optim.Optimizer,
#                 device: str = 'cpu',
#                 filename_prefix: str = 'model',
#                 event_frequency: float = 1.0,
#                 train_set: torch.utils.data.DataLoader = None,
#                 val_set=None,
#                 stopper=None,
#                 loss_fn=recognition_loss_fn,
#                 evaluator=recognition_evaluator_fn):
#        self.model = model
#        self.optimizer = optimizer
#        self.device = device
#        self.filename_prefix = filename_prefix
#        self.event_frequency = event_frequency
#        self.event_it = int(len(train_set) * event_frequency)
#        self.train_set = train_set
#        self.val_set = val_set
#        self.stopper = stopper if stopper else NoStopping()
#        self.iterations = 0
#        self.lr_scheduler = None
#        self.loss_fn = loss_fn
#        self.evaluator = evaluator
#        # fill training metadata fields in model files
#        self.model.seg_type = train_set.dataset.seg_type
#
#    def add_lr_scheduler(self, lr_scheduler: TrainScheduler):
#        self.lr_scheduler = lr_scheduler
#
#    def run(self, event_callback=lambda *args, **kwargs: None, iteration_callback=lambda *args, **kwargs: None):
#        logger.debug('Moving model to device {}'.format(self.device))
#        self.model.to(self.device)
#        self.model.train()
#
#        logger.debug('Starting up training...')
#
#        if 'accuracy' not in self.model.user_metadata:
#            self.model.user_metadata['accuracy'] = []
#
#        while self.stopper.trigger():
#            for _, batch in zip(range(self.event_it), self.train_set):
#                input, target = batch['image'], batch['target']
#                input = input.to(self.device, non_blocking=True)
#                target = target.to(self.device, non_blocking=True)
#                input = input.requires_grad_()
#                # sequence batch
#                if 'seq_lens' in batch:
#                    seq_lens, label_lens = batch['seq_lens'], batch['target_lens']
#                    seq_lens = seq_lens.to(self.device, non_blocking=True)
#                    label_lens = label_lens.to(self.device, non_blocking=True)
#                    target = (target, label_lens)
#                    o = self.model.nn(input, seq_lens)
#                else:
#                    o = self.model.nn(input)
#                self.optimizer.zero_grad()
#                loss = self.loss_fn(self.model.criterion, o, target)
#                if not torch.isinf(loss):
#                    loss.backward()
#                    self.optimizer.step()
#                else:
#                    logger.debug('infinite loss in trial')
#                if self.lr_scheduler:
#                    self.lr_scheduler.batch_step(loss=loss)
#                iteration_callback()
#                # prevent memory leak
#                del loss, o
#            self.iterations += self.event_it
#            logger.debug('Starting evaluation run')
#            eval_res = self.evaluator(self.model, self.val_set, self.device)
#            if self.lr_scheduler:
#                self.lr_scheduler.epoch_step(val_loss=eval_res['val_metric'])
#            self.stopper.update(eval_res['val_metric'])
#            self.model.user_metadata['accuracy'].append((self.iterations, float(eval_res['val_metric'])))
#            logger.info('Saving to {}_{}'.format(self.filename_prefix, self.stopper.epoch))
#            # fill one_channel_mode after 1 iteration over training data set
#            im_mode = self.train_set.dataset.im_mode
#            if im_mode in ['1', 'L']:
#                self.model.one_channel_mode = im_mode
#            try:
#                self.model.hyper_params['completed_epochs'] = self.stopper.epoch
#                self.model.save_model('{}_{}.mlmodel'.format(self.filename_prefix, self.stopper.epoch))
#            except Exception as e:
#                logger.error('Saving model failed: {}'.format(str(e)))
#            event_callback(epoch=self.stopper.epoch, **eval_res)
#
#    @classmethod
#    def load_model(cls, model_path: str,
#                   load_hyper_parameters: Optional[bool] = False,
#                   message: Callable[[str], None] = lambda *args, **kwargs: None):
#        logger.info(f'Loading existing model from {model_path} ')
#        message(f'Loading existing model from {model_path} ', nl=False)
#        nn = vgsl.TorchVGSLModel.load_model(model_path)
#        if load_hyper_parameters:
#            hyper_params = nn.hyper_params
#        else:
#            hyper_params = {}
#        message('\u2713', fg='green', nl=False)
#        return nn, hyper_params
#
#    @classmethod
#    def recognition_train_gen(cls,
#                              hyper_params: Dict = None,
#                              progress_callback: Callable[[str, int], Callable[[None], None]] = lambda string, length: lambda: None,
#                              message: Callable[[str], None] = lambda *args, **kwargs: None,
#                              output: str = 'model',
#                              spec: str = default_specs.RECOGNITION_SPEC,
#                              append: Optional[int] = None,
#                              load: Optional[Union[pathlib.Path, str]] = None,
#                              device: str = 'cpu',
#                              reorder: Union[bool, str] = True,
#                              training_data: Union[Sequence[Union[pathlib.Path, str]], Sequence[Dict[str, Any]]] = None,
#                              evaluation_data: Optional[Union[Sequence[Union[pathlib.Path, str]], Sequence[Dict[str, Any]]]] = None,
#                              partition: Optional[float] = None,
#                              preload: Optional[bool] = None,
#                              threads: int = 1,
#                              load_hyper_parameters: bool = False,
#                              repolygonize: bool = False,
#                              force_binarization: bool = False,
#                              format_type: str = 'path',
#                              codec: Optional[Dict] = None,
#                              resize: str = 'fail',
#                              augment: bool = False):
#        """
#        This is an ugly constructor that takes all the arguments from the command
#        line driver, finagles the datasets, models, and hyperparameters correctly
#        and returns a KrakenTrainer object.
#
#        Setup parameters (load, training_data, evaluation_data, ....) are named,
#        model hyperparameters (everything in
#        kraken.lib.default_specs.RECOGNITION_HYPER_PARAMS) are in in the
#        `hyper_params` argument.
#
#        Args:
#            hyper_params (dict): Hyperparameter dictionary containing all fields
#                                 from
#                                 kraken.lib.default_specs.RECOGNITION_HYPER_PARAMS
#            progress_callback (Callable): Callback for progress reports on various
#                                          computationally expensive processes. A
#                                          human readable string and the process
#                                          length is supplied. The callback has to
#                                          return another function which will be
#                                          executed after each step.
#            message (Callable): Messaging printing method for above log but below
#                                warning level output, i.e. infos that should
#                                generally be shown to users.
#            **kwargs: Setup parameters, i.e. CLI parameters of the train() command.
#
#        Returns:
#            A KrakenTrainer object.
#        """
#
#        hyper_params_ = default_specs.RECOGNITION_HYPER_PARAMS
#
#        # load model if given. if a new model has to be created we need to do that
#        # after data set initialization, otherwise to output size is still unknown.
#        if load:
#            nn, hp = cls.load_model(load,
#                                    load_hyper_parameters=load_hyper_parameters,
#                                    message=message)
#            hyper_params_.update(hp)
#        else:
#            nn = None
#
#        if hyper_params:
#            hyper_params_.update(hyper_params)
#
#        validate_hyper_parameters(hyper_params_)
#
#        hyper_params = hyper_params_
#
#        logger.info('Hyperparameters')
#        for k, v in hyper_params.items():
#            logger.info(f'{k}:\t{v}')
#
#        if len(training_data) > 2500 and not preload:
#            logger.info('Disabling preloading for large (>2500) training data set. Enable by setting --preload parameter')
#            preload = False
#        # implicit preloading enabled for small data sets
#        if preload is None:
#            preload = True
#
#        # set multiprocessing tensor sharing strategy
#        if 'file_system' in torch.multiprocessing.get_all_sharing_strategies():
#            logger.debug('Setting multiprocessing tensor sharing strategy to file_system')
#            torch.multiprocessing.set_sharing_strategy('file_system')
#
#        gt_set = DatasetClass(normalization=hyper_params['normalization'],
#                              whitespace_normalization=hyper_params['normalize_whitespace'],
#                              reorder=reorder,
#                              im_transforms=transforms,
#                              preload=preload,
#                              augmentation=hyper_params['augment'])
#        bar = progress_callback('Building training set', len(training_data))
#
#        if (threads and threads > 1) and format_type != 'binary':
#            with Pool(processes=threads) as pool:
#                for im in pool.imap_unordered(partial(_star_fun, gt_set.parse), training_data, 5):
#                    logger.debug(f'Adding line {im} to training set')
#                    if im:
#                        gt_set.add(**im)
#                    bar()
#        else:
#            for im in training_data:
#                try:
#                    gt_set.add(**im)
#                except KrakenInputException as e:
#                    logger.warning(str(e))
#                bar()
#
#        if evaluation_data:
#            val_set = DatasetClass(normalization=hyper_params['normalization'],
#                                   whitespace_normalization=hyper_params['normalize_whitespace'],
#                                   reorder=reorder,
#                                   im_transforms=transforms,
#                                   preload=preload)
#
#            bar = progress_callback('Building validation set', len(evaluation_data))
#
#            if (threads and threads > 1) and format_type != 'binary':
#                with Pool(processes=threads) as pool:
#                    for im in pool.imap_unordered(partial(_star_fun, val_set.parse), evaluation_data, 5):
#                        logger.debug(f'Adding line {im} to validation set')
#                        if im:
#                            val_set.add(**im)
#                        bar()
#            else:
#                for im in evaluation_data:
#                    try:
#                        val_set.add(**im)
#                    except KrakenInputException as e:
#                        logger.warning(str(e))
#                    bar()
#        else:
#            train_len = int(len(gt_set)*partition)
#            val_len = len(gt_set) - train_len
#            logger.info(f'No explicit validation data provided. Splitting off '
#                        f'{val_len} (of {len(gt_set)}) samples to validation set.')
#            gt_set, val_set = random_split(gt_set, (train_len, val_len))
#
#        if len(gt_set) == 0:
#            logger.error('No valid training data was provided to the train command. Please add valid XML or line data.')
#            return None
#
#        logger.info(f'Training set {len(gt_set)} lines, validation set '
#                    f'{len(val_set)} lines, alphabet {len(gt_set.alphabet)} '
#                    'symbols')
#        alpha_diff_only_train = set(gt_set.alphabet).difference(set(val_set.alphabet))
#        alpha_diff_only_val = set(val_set.alphabet).difference(set(gt_set.alphabet))
#        if alpha_diff_only_train:
#            logger.warning(f'alphabet mismatch: chars in training set only: '
#                           f'{alpha_diff_only_train} (not included in accuracy test '
#                           'during training)')
#        if alpha_diff_only_val:
#            logger.warning(f'alphabet mismatch: chars in validation set only: {alpha_diff_only_val} (not trained)')
#        logger.info('grapheme\tcount')
#        for k, v in sorted(gt_set.alphabet.items(), key=lambda x: x[1], reverse=True):
#            char = make_printable(k)
#            if char == k:
#                char = '\t' + char
#            logger.info(f'{char}\t{v}')
#
#        if codec:
#            logger.info('Instantiating codec')
#            codec = PytorchCodec(codec)
#            for k, v in codec.c2l.items():
#                char = make_printable(k)
#                if char == k:
#                    char = '\t' + char
#                logger.info(f'{char}\t{v}')
#
#        logger.info('Encoding training set')
#
#        # half the number of data loading processes if device isn't cuda and we haven't enabled preloading
#
#        if device == 'cpu' and not preload:
#            loader_threads = threads // 2
#        else:
#            loader_threads = threads
#        train_loader = InfiniteDataLoader(gt_set, batch_size=hyper_params['batch_size'],
#                                          shuffle=True,
#                                          num_workers=loader_threads,
#                                          pin_memory=True,
#                                          collate_fn=collate_sequences)
#        threads = max(threads - loader_threads, 1)
#
#        # don't encode validation set as the alphabets may not match causing encoding failures
#        val_set.no_encode()
#        val_loader = DataLoader(val_set,
#                                batch_size=hyper_params['batch_size'],
#                                num_workers=loader_threads,
#                                pin_memory=True,
#                                collate_fn=collate_sequences)
#
#        logger.debug('Constructing {} optimizer (lr: {}, momentum: {})'.format(
#            hyper_params['optimizer'], hyper_params['lrate'], hyper_params['momentum']))
#
#        # updates model's hyper params with users defined
#        nn.hyper_params = hyper_params
#
#        # set model type metadata field
#        nn.model_type = 'recognition'
#
#        # set mode to trainindg
#        nn.train()
#
#        # set number of OpenMP threads
#        logger.debug(f'Set OpenMP threads to {threads}')
#        nn.set_num_threads(threads)
#
#        if hyper_params['optimizer'] == 'Adam':
#            optim = torch.optim.Adam(nn.nn.parameters(), lr=hyper_params['lrate'], weight_decay=hyper_params['weight_decay'])
#        else:
#            optim = getattr(torch.optim, hyper_params['optimizer'])(nn.nn.parameters(),
#                                                                    lr=hyper_params['lrate'],
#                                                                    momentum=hyper_params['momentum'],
#                                                                    weight_decay=hyper_params['weight_decay'])
#
#        if 'seg_type' not in nn.user_metadata:
#            nn.user_metadata['seg_type'] = 'baselines' if format_type != 'path' else 'bbox'
#
#        tr_it = TrainScheduler(optim)
#        if hyper_params['schedule'] == '1cycle':
#            annealing_one = partial(annealing_onecycle,
#                                    max_lr=hyper_params['lrate'],
#                                    epochs=hyper_params['step_size'],
#                                    steps_per_epoch=len(gt_set))
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_one)
#        elif hyper_params['schedule'] == 'exponential':
#            annealing_exp = partial(annealing_exponential,
#                                    step_size=hyper_params['step_size'],
#                                    gamma=hyper_params['gamma'])
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_exp)
#        elif hyper_params['schedule'] == 'step':
#            annealing_step_p = partial(annealing_step,
#                                       step_size=hyper_params['step_size'],
#                                       gamma=hyper_params['gamma'])
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_step_p)
#        elif hyper_params['schedule'] == 'reduceonplateau':
#            annealing_red = partial(annealing_reduceonplateau,
#                                    patience=hyper_params['rop_patience'],
#                                    factor=hyper_params['gamma'])
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_red)
#        elif hyper_params['schedule'] == 'cosine':
#            annealing_cos = partial(annealing_cosine,
#                                    t_max=hyper_params['cos_t_max'])
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_cos)
#        else:
#            # constant learning rate scheduler
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_const)
#
#        if hyper_params['quit'] == 'early':
#            st_it = EarlyStopping(hyper_params['min_delta'], hyper_params['lag'])
#        elif hyper_params['quit'] == 'dumb':
#            st_it = EpochStopping(hyper_params['epochs'] - hyper_params['completed_epochs'])
#        else:
#            logger.error(f'Invalid training interruption scheme {quit}')
#            return None
#
#        trainer = cls(model=nn,
#                      optimizer=optim,
#                      device=device,
#                      filename_prefix=output,
#                      event_frequency=hyper_params['freq'],
#                      train_set=train_loader,
#                      val_set=val_loader,
#                      stopper=st_it)
#
#        trainer.add_lr_scheduler(tr_it)
#
#        return trainer
#
#    @classmethod
#    def segmentation_train_gen(cls,
#                               hyper_params: Dict = None,
#                               load_hyper_parameters: bool = False,
#                               progress_callback: Callable[[str, int], Callable[[None], None]] = lambda string, length: lambda: None,
#                               message: Callable[[str], None] = lambda *args, **kwargs: None,
#                               output: str = 'model',
#                               spec: str = default_specs.SEGMENTATION_SPEC,
#                               load: Optional[str] = None,
#                               device: str = 'cpu',
#                               training_data: Union[Sequence[Union[pathlib.Path, str]], Sequence[Dict[str, Any]]] = None,
#                               evaluation_data: Optional[Union[Sequence[Union[pathlib.Path, str]], Sequence[Dict[str, Any]]]] = None,
#                               partition: Optional[float] = None,
#                               threads: int = 1,
#                               force_binarization: bool = False,
#                               format_type: str = 'path',
#                               suppress_regions: bool = False,
#                               suppress_baselines: bool = False,
#                               valid_regions: Optional[Sequence[str]] = None,
#                               valid_baselines: Optional[Sequence[str]] = None,
#                               merge_regions: Optional[Dict[str, str]] = None,
#                               merge_baselines: Optional[Dict[str, str]] = None,
#                               bounding_regions: Optional[Sequence[str]] = None,
#                               resize: str = 'fail',
#                               augment: bool = False,
#                               topline: Union[bool, None] = False):
#        """
#        This is an ugly constructor that takes all the arguments from the command
#        line driver, finagles the datasets, models, and hyperparameters correctly
#        and returns a KrakenTrainer object.
#
#        Setup parameters (load, training_data, evaluation_data, ....) are named,
#        model hyperparameters (everything in
#        kraken.lib.default_specs.SEGMENTATION_HYPER_PARAMS) are in in the
#        `hyper_params` argument.
#
#        Args:
#            hyper_params (dict): Hyperparameter dictionary containing all fields
#                                 from
#                                 kraken.lib.default_specs.SEGMENTATION_HYPER_PARAMS
#            progress_callback (Callable): Callback for progress reports on various
#                                          computationally expensive processes. A
#                                          human readable string and the process
#                                          length is supplied. The callback has to
#                                          return another function which will be
#                                          executed after each step.
#            message (Callable): Messaging printing method for above log but below
#                                warning level output, i.e. infos that should
#                                generally be shown to users.
#            **kwargs: Setup parameters, i.e. CLI parameters of the train() command.
#
#        Returns:
#            A KrakenTrainer object.
#        """
#        # load model if given. if a new model has to be created we need to do that
#        # after data set initialization, otherwise to output size is still unknown.
#        nn = None
#
#        hyper_params_ = default_specs.SEGMENTATION_HYPER_PARAMS
#
#        if load:
#            nn, hp = cls.load_model(load,
#                                    load_hyper_parameters=load_hyper_parameters,
#                                    message=message)
#            hyper_params_.update(hp)
#            batch, channels, height, width = nn.input
#        else:
#            # preparse input sizes from vgsl string to seed ground truth data set
#            # sizes and dimension ordering.
#            spec = spec.strip()
#            if spec[0] != '[' or spec[-1] != ']':
#                logger.error(f'VGSL spec "{spec}" not bracketed')
#                return None
#            blocks = spec[1:-1].split(' ')
#            m = re.match(r'(\d+),(\d+),(\d+),(\d+)', blocks[0])
#            if not m:
#                logger.error(f'Invalid input spec {blocks[0]}')
#                return None
#            batch, height, width, channels = [int(x) for x in m.groups()]
#
#        if hyper_params:
#            hyper_params_.update(hyper_params)
#
#        validate_hyper_parameters(hyper_params_)
#
#        hyper_params = hyper_params_
#
#        transforms = ImageInputTransforms(batch, height, width, channels, 0, valid_norm=False)
#
#        # set multiprocessing tensor sharing strategy
#        if 'file_system' in torch.multiprocessing.get_all_sharing_strategies():
#            logger.debug('Setting multiprocessing tensor sharing strategy to file_system')
#            torch.multiprocessing.set_sharing_strategy('file_system')
#
#        if not valid_regions:
#            valid_regions = None
#        if not valid_baselines:
#            valid_baselines = None
#
#        if suppress_regions:
#            valid_regions = []
#            merge_regions = None
#        if suppress_baselines:
#            valid_baselines = []
#            merge_baselines = None
#
#        gt_set = BaselineSet(training_data,
#                             line_width=hyper_params['line_width'],
#                             im_transforms=transforms,
#                             mode=format_type,
#                             augmentation=hyper_params['augment'],
#                             valid_baselines=valid_baselines,
#                             merge_baselines=merge_baselines,
#                             valid_regions=valid_regions,
#                             merge_regions=merge_regions)
#        if not evaluation_data:
#            evaluation_data = []
#
#        val_set = BaselineSet(evaluation_data,
#                              line_width=hyper_params['line_width'],
#                              im_transforms=transforms,
#                              mode=format_type,
#                              augmentation=hyper_params['augment'],
#                              valid_baselines=valid_baselines,
#                              merge_baselines=merge_baselines,
#                              valid_regions=valid_regions,
#                              merge_regions=merge_regions)
#
#        if format_type is None:
#            for page in training_data:
#                gt_set.add(**page)
#            for page in evaluation_data:
#                val_set.add(**page)
#
#        # overwrite class mapping in validation set
#        val_set.num_classes = gt_set.num_classes
#        val_set.class_mapping = gt_set.class_mapping
#
#        if not evaluation_data:
#            train_len = int(len(gt_set)*partition)
#            val_len = len(gt_set) - train_len
#            logger.info(f'No explicit validation data provided. Splitting off '
#                        f'{val_len} (of {len(gt_set)}) samples to validation set.')
#            gt_set, val_set = random_split(gt_set, (train_len, val_len))
#
#        if not load:
#            spec = f'[{spec[1:-1]} O2l{gt_set.num_classes}]'
#            message(f'Creating model {spec} with {gt_set.num_classes} outputs ', nl=False)
#            nn = vgsl.TorchVGSLModel(spec)
#            message('\u2713', fg='green')
#            if bounding_regions is not None:
#                nn.user_metadata['bounding_regions'] = bounding_regions
#            nn.user_metadata['topline'] = topline
#        else:
#            if gt_set.class_mapping['baselines'].keys() != nn.user_metadata['class_mapping']['baselines'].keys() or \
#               gt_set.class_mapping['regions'].keys() != nn.user_metadata['class_mapping']['regions'].keys():
#
#                bl_diff = set(gt_set.class_mapping['baselines'].keys()).symmetric_difference(
#                    set(nn.user_metadata['class_mapping']['baselines'].keys()))
#                regions_diff = set(gt_set.class_mapping['regions'].keys()).symmetric_difference(
#                    set(nn.user_metadata['class_mapping']['regions'].keys()))
#
#                if resize == 'fail':
#                    logger.error(f'Training data and model class mapping differ (bl: {bl_diff}, regions: {regions_diff}')
#                    raise KrakenInputException(
#                        f'Training data and model class mapping differ (bl: {bl_diff}, regions: {regions_diff}')
#                elif resize == 'add':
#                    new_bls = gt_set.class_mapping['baselines'].keys() - nn.user_metadata['class_mapping']['baselines'].keys()
#                    new_regions = gt_set.class_mapping['regions'].keys() - nn.user_metadata['class_mapping']['regions'].keys()
#                    cls_idx = max(max(nn.user_metadata['class_mapping']['baselines'].values()) if nn.user_metadata['class_mapping']['baselines'] else -1,
#                                  max(nn.user_metadata['class_mapping']['regions'].values()) if nn.user_metadata['class_mapping']['regions'] else -1)
#                    message(f'Adding {len(new_bls) + len(new_regions)} missing types to network output layer ', nl=False)
#                    nn.resize_output(cls_idx + len(new_bls) + len(new_regions) + 1)
#                    for c in new_bls:
#                        cls_idx += 1
#                        nn.user_metadata['class_mapping']['baselines'][c] = cls_idx
#                    for c in new_regions:
#                        cls_idx += 1
#                        nn.user_metadata['class_mapping']['regions'][c] = cls_idx
#                    message('\u2713', fg='green')
#                elif resize == 'both':
#                    message('Fitting network exactly to training set ', nl=False)
#                    new_bls = gt_set.class_mapping['baselines'].keys() - nn.user_metadata['class_mapping']['baselines'].keys()
#                    new_regions = gt_set.class_mapping['regions'].keys() - nn.user_metadata['class_mapping']['regions'].keys()
#                    del_bls = nn.user_metadata['class_mapping']['baselines'].keys() - gt_set.class_mapping['baselines'].keys()
#                    del_regions = nn.user_metadata['class_mapping']['regions'].keys() - gt_set.class_mapping['regions'].keys()
#
#                    message(f'Adding {len(new_bls) + len(new_regions)} missing '
#                            f'types and removing {len(del_bls) + len(del_regions)} to network output layer ',
#                            nl=False)
#                    cls_idx = max(max(nn.user_metadata['class_mapping']['baselines'].values()) if nn.user_metadata['class_mapping']['baselines'] else -1,
#                                  max(nn.user_metadata['class_mapping']['regions'].values()) if nn.user_metadata['class_mapping']['regions'] else -1)
#
#                    del_indices = [nn.user_metadata['class_mapping']['baselines'][x] for x in del_bls]
#                    del_indices.extend(nn.user_metadata['class_mapping']['regions'][x] for x in del_regions)
#                    nn.resize_output(cls_idx + len(new_bls) + len(new_regions) -
#                                     len(del_bls) - len(del_regions) + 1, del_indices)
#
#                    # delete old baseline/region types
#                    cls_idx = min(min(nn.user_metadata['class_mapping']['baselines'].values()) if nn.user_metadata['class_mapping']['baselines'] else np.inf,
#                                  min(nn.user_metadata['class_mapping']['regions'].values()) if nn.user_metadata['class_mapping']['regions'] else np.inf)
#
#                    bls = {}
#                    for k, v in sorted(nn.user_metadata['class_mapping']['baselines'].items(), key=lambda item: item[1]):
#                        if k not in del_bls:
#                            bls[k] = cls_idx
#                            cls_idx += 1
#
#                    regions = {}
#                    for k, v in sorted(nn.user_metadata['class_mapping']['regions'].items(), key=lambda item: item[1]):
#                        if k not in del_regions:
#                            regions[k] = cls_idx
#                            cls_idx += 1
#
#                    nn.user_metadata['class_mapping']['baselines'] = bls
#                    nn.user_metadata['class_mapping']['regions'] = regions
#
#                    # add new baseline/region types
#                    cls_idx -= 1
#                    for c in new_bls:
#                        cls_idx += 1
#                        nn.user_metadata['class_mapping']['baselines'][c] = cls_idx
#                    for c in new_regions:
#                        cls_idx += 1
#                        nn.user_metadata['class_mapping']['regions'][c] = cls_idx
#                    message('\u2713', fg='green')
#                else:
#                    logger.error(f'invalid resize parameter value {resize}')
#                    raise KrakenInputException(f'invalid resize parameter value {resize}')
#            # backfill gt_set/val_set mapping if key-equal as the actual
#            # numbering in the gt_set might be different
#            gt_set.class_mapping = nn.user_metadata['class_mapping']
#            val_set.class_mapping = nn.user_metadata['class_mapping']
#
#        # updates model's hyper params with users defined
#        nn.hyper_params = hyper_params
#
#        # change topline/baseline switch
#        loc = {None: 'centerline',
#               True: 'topline',
#               False: 'baseline'}
#
#        if 'topline' not in nn.user_metadata:
#            logger.warning(f'Setting baseline location to {loc[topline]} from unset model.')
#        elif nn.user_metadata['topline'] != topline:
#            from_loc = loc[nn.user_metadata["topline"]]
#            logger.warning(f'Changing baseline location from {from_loc} to {loc[topline]}.')
#        nn.user_metadata['topline'] = topline
#
#        message('Training line types:')
#        for k, v in gt_set.class_mapping['baselines'].items():
#            message(f'  {k}\t{v}\t{gt_set.class_stats["baselines"][k]}')
#        message('Training region types:')
#        for k, v in gt_set.class_mapping['regions'].items():
#            message(f'  {k}\t{v}\t{gt_set.class_stats["regions"][k]}')
#
#        if len(gt_set.imgs) == 0:
#            logger.error('No valid training data was provided to the train command. Please add valid XML data.')
#            return None
#
#        if device == 'cpu':
#            loader_threads = threads // 2
#        else:
#            loader_threads = threads
#
#        train_loader = InfiniteDataLoader(gt_set, batch_size=1, shuffle=True, num_workers=loader_threads, pin_memory=True)
#        val_loader = DataLoader(val_set, batch_size=1, shuffle=True, num_workers=loader_threads, pin_memory=True)
#        threads = max((threads - loader_threads, 1))
#
#        # set model type metadata field and dump class_mapping
#        nn.model_type = 'segmentation'
#        nn.user_metadata['class_mapping'] = val_set.class_mapping
#
#        # set mode to training
#        nn.train()
#
#        logger.debug(f'Set OpenMP threads to {threads}')
#        nn.set_num_threads(threads)
#
#        if hyper_params['optimizer'] == 'Adam':
#            optim = torch.optim.Adam(nn.nn.parameters(), lr=hyper_params['lrate'], weight_decay=hyper_params['weight_decay'])
#        else:
#            optim = getattr(torch.optim, hyper_params['optimizer'])(nn.nn.parameters(),
#                                                                    lr=hyper_params['lrate'],
#                                                                    momentum=hyper_params['momentum'],
#                                                                    weight_decay=hyper_params['weight_decay'])
#
#        tr_it = TrainScheduler(optim)
#        if hyper_params['schedule'] == '1cycle':
#            annealing_one = partial(annealing_onecycle,
#                                    max_lr=hyper_params['lrate'],
#                                    epochs=hyper_params['epochs'],
#                                    steps_per_epoch=len(gt_set))
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_one)
#        elif hyper_params['schedule'] == 'exponential':
#            annealing_exp = partial(annealing_exponential,
#                                    step_size=hyper_params['step_size'],
#                                    gamma=hyper_params['gamma'])
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_exp)
#        elif hyper_params['schedule'] == 'step':
#            annealing_step_p = partial(annealing_step,
#                                       step_size=hyper_params['step_size'],
#                                       gamma=hyper_params['gamma'])
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_step_p)
#        elif hyper_params['schedule'] == 'reduceonplateau':
#            annealing_red = partial(annealing_reduceonplateau,
#                                    patience=hyper_params['rop_patience'],
#                                    factor=hyper_params['gamma'])
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_red)
#        elif hyper_params['schedule'] == 'cosine':
#            annealing_cos = partial(annealing_cosine, t_max=hyper_params['cos_t_max'])
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_cos)
#        else:
#            # constant learning rate scheduler
#            tr_it.add_phase(int(len(gt_set) * hyper_params['epochs']),
#                            annealing_const)
#
#        if hyper_params['quit'] == 'early':
#            st_it = EarlyStopping(hyper_params['min_delta'], hyper_params['lag'])
#        elif hyper_params['quit'] == 'dumb':
#            st_it = EpochStopping(hyper_params['epochs'] - hyper_params['completed_epochs'])
#        else:
#            logger.error(f'Invalid training interruption scheme {quit}')
#            return None
#
#        trainer = cls(model=nn,
#                      optimizer=optim,
#                      device=device,
#                      filename_prefix=output,
#                      event_frequency=hyper_params['freq'],
#                      train_set=train_loader,
#                      val_set=val_loader,
#                      stopper=st_it,
#                      loss_fn=baseline_label_loss_fn,
#                      evaluator=baseline_label_evaluator_fn)
#
#        trainer.add_lr_scheduler(tr_it)
#
#        return trainer
