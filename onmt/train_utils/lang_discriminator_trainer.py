from __future__ import division

import sys, tempfile
import onmt
import onmt.Markdown
import onmt.modules
import argparse
import torch
import torch.nn as nn
from torch import cuda
from torch.autograd import Variable
import math
import time, datetime
import random
import numpy as np
from onmt.multiprocessing.multiprocessing_wrapper import MultiprocessingRunner
from onmt.ModelConstructor import init_model_parameters
from onmt.train_utils.trainer import BaseTrainer
from onmt.Stats import Logger
from statistics import mean, stdev
from onmt.ModelConstructor import build_model, init_model_parameters



class DynamicLossScaler:

    def __init__(self, init_scale=2 ** 7, scale_factor=2., scale_window=2000):
        self.loss_scale = init_scale
        self.scale_factor = scale_factor
        self.scale_window = scale_window
        self._iter = 0
        self._last_overflow_iter = -1

    # we will pass the iter of optim to here
    def update_scale(self, overflow):

        self._iter += 1
        if overflow:
            self.loss_scale /= self.scale_factor
            self._last_overflow_iter = self._iter
        elif (self._iter - self._last_overflow_iter) % self.scale_window == 0:
            self.loss_scale *= self.scale_factor

    @staticmethod
    def has_overflow(grad_norm):
        # detect inf and nan
        if grad_norm == float('inf') or grad_norm != grad_norm:
            return True
        return False


class LanguageDiscriminatorTrainer(BaseTrainer):

    def __init__(self, cls_model, loss_function, train_data, valid_data, dicts, opt, nmt_type='transformer'):
        super().__init__(cls_model, loss_function, train_data, valid_data, dicts, opt)
        self.optim = onmt.Optim(opt)
        self.scaler = DynamicLossScaler(opt.fp16_loss_scale, scale_window=2000)
        self.n_samples = 1
        self.start_time = time.time()
        # self.nmt_model = nmt_model
        self.nmt_type = nmt_type
        self.encoder_length_type = opt.encoder_length_type

        if self.cuda:
            torch.cuda.set_device(self.opt.gpus[0])
            torch.manual_seed(self.opt.seed)
            # Important:
            # Loss function needs to be in fp32
            self.loss_function = self.loss_function.cuda()
            self.model = self.model.cuda()
            # self.nmt_model = self.nmt_model.cuda()

        # prepare some meters
        self.logger = Logger(self.optim, scaler=self.scaler)
        self.meters = self.logger.meters

    # fp16 utility
    def convert_fp16(self, model_state=None, optim_state=None):

        if model_state is not None:
            self.model.load_state_dict(model_state)

        self.model = self.model.half()
        self.nmt_model = self.nmt_model.half()
        params = [p for p in self.model.parameters() if p.requires_grad]
        total_param_size = sum(p.data.numel() for p in params)

        self.fp32_params = params[0].new(0).float().new(total_param_size)

        # now we transfer the params from fp16 over fp32
        offset = 0
        for p in params:
            numel = p.data.numel()
            self.fp32_params[offset:offset + numel].copy_(p.data.view(-1))
            offset += numel

        self.fp32_params = torch.nn.Parameter(self.fp32_params)
        self.fp32_params.grad = self.fp32_params.data.new(total_param_size).zero_()
        # we optimize on the fp32 params
        self.optim.set_parameters([self.fp32_params])

        if optim_state is not None:
            self.optim.load_state_dict(optim_state)

        print(self.optim.optimizer)

    # fp32 utility (gradients and optim)
    def convert_fp32(self, model_state=None, optim_state=None):

        if model_state is not None:
            self.model.load_state_dict(model_state)

        params = [p for p in self.model.parameters() if p.requires_grad]
        self.optim.set_parameters(params)

        if optim_state is not None:
            self.optim.load_state_dict(optim_state)

        print(self.optim.optimizer)

    def eval(self, data):

        total_loss = 0
        total_words = 0
        total_batch_size = 0

        num_correct = 0
        total = 0

        # batch_order = data.create_order(random=False)
        torch.cuda.empty_cache()
        self.model.eval()

        """ New semantics of PyTorch: not creating gradients in this mode """

        with torch.no_grad():
            for i in range(len(data)):

                samples = data.next()

                batch = samples[0]
                batch.cuda()

                """ outputs can be either 
                        hidden states from decoder or
                        prob distribution from decoder generator
                    during Evaluation we sample from the prior distribution
                    and don't use sampling
                """

                src = batch.get('source')
                src_length = batch.get('src_length')
                target = batch.get('src_attbs')

                # print(src)
                # print(target)
                src = src.transpose(0, 1)

                src_context, _  = self.nmt_model.encoder(src)

                if self.encoder_length_type == 'fix':
                    src = src.new(src_context.size(1), src_context.size(0)).fill_(onmt.Constants.BOS)

                outputs = self.model(src, src_context)

                loss_output = self.loss_function(outputs, target)

                loss_data = loss_output.sum().item()

                total_batch_size += batch.size
                total_words += batch.tgt_size

                total_loss += loss_data

                # now compute the accuracy
                pred = outputs.max(1)[1]
                num_correct += pred.eq(target).sum().item()
                total += batch.size


        self.model.train()

        acc = num_correct / total * 100
        print("Accuracy : %.2f percent" % acc)
        # take the average

        return total_loss / (total_words + 1e-6)

    def train_epoch(self, epoch, resume=False, batch_order=None, iteration=0):

        opt = self.opt
        train_data = self.train_data
        print("TRAINING EPOCH")

        # Clear the gradients of the model
        self.model.zero_grad()
        self.optim.zero_grad()

        if opt.extra_shuffle and epoch > opt.curriculum:
            train_data.shuffle()

        # Shuffle mini batch order.
        if resume:
            train_data.batch_order = batch_order
            train_data.set_index(iteration)
            print("Resuming from iteration: %d" % iteration)
        else:
            batch_order = train_data.create_order()
            iteration = 0

        self.logger.reset()
        nSamples = len(train_data)

        counter = 0
        num_accumulated_words = 0
        num_accumulated_sents = 0

        for i in range(iteration, nSamples):

            curriculum = (epoch < opt.curriculum)

            samples = train_data.next(curriculum=curriculum)

            oom = False
            try:
                batch = samples[0]
                batch.cuda()

                src = batch.get('source')
                src_length = batch.get('src_length')
                target = batch.get('src_attbs')

                with torch.no_grad():
                    src = src.transpose(0, 1)
                    src_context, _ = self.nmt_model.encoder(src)

                    if self.encoder_length_type == 'fix':
                        src = src.new(src_context.size(1), src_context.size(0)).fill_(onmt.Constants.BOS)

                outputs = self.model( src, src_context.detach())
                # print("DEBUGGING 2")
                loss_output = self.loss_function(outputs, target)

                loss = loss_output.sum()

                ## Scale UP the loss so that the gradients are not cutoff
                if self.opt.fp16:
                    normalizer = 1.0 / self.scaler.loss_scale
                else:
                    normalizer = 1.0

                loss.div(normalizer).backward()

                ## take the negative likelihood
                loss_data = loss.item()

                pred = outputs.max(1)[1]
                num_correct = pred.eq(target).sum().item()
                # total = target.sum().item() # should be batch_size

                self.meters['total_lang_correct'].update(num_correct)
                self.meters["total_sents"].update(batch.size)


            except RuntimeError as e:
                if 'out of memory' in str(e) or 'get_temporary_buffer' in str(e):
                    oom = True
                    self.reset_state()
                    torch.cuda.empty_cache()
                    self.meters['oom'].update(1)
                else:
                    raise e

            if not oom:
                # print("DEBUGGING 2")
                src_size = batch.src_size
                tgt_size = batch.tgt_size
                batch_size = batch.size

                counter = counter + 1
                num_accumulated_words += tgt_size
                num_accumulated_sents += batch_size

                # We only update the parameters after getting gradients from n mini-batches
                # simulating the multi-gpu situation
                normalizer = num_accumulated_words if opt.normalize_gradient else 1
                if num_accumulated_words >= opt.batch_size_update:
                    # Update the parameters.

                    if self.opt.fp16:
                        # First we have to copy the grads from fp16 to fp32
                        self._get_flat_grads(out=self.fp32_params.grad)

                        normalizer = normalizer * self.scaler.loss_scale
                        # rescale and clip grads
                        self.fp32_params.grad.data.div_(normalizer)

                        grad_norm = torch.norm(self.fp32_params.grad.data).item()

                        overflow = DynamicLossScaler.has_overflow(grad_norm)
                        self.scaler.update_scale(overflow)
                    else:
                        overflow = False

                    if overflow:
                        if self.scaler.loss_scale <= 1e-4:
                            raise Exception((
                                            'Minimum loss scale reached ({}). Your loss is probably exploding. '
                                            'Try lowering the learning rate, using gradient clipping or '
                                            'increasing the batch size.'
                                            ).format(1e-4))
                        print('setting loss scale to: ' + str(self.scaler.loss_scale))
                        self.model.zero_grad()
                        self.optim.zero_grad()
                        num_accumulated_words = 0
                        num_accumulated_sents = 0
                        loss_data = 0
                        grad_norm = 0
                        self.meters['gnorm'].reset()
                    else:
                        try:
                            # max_norm = self.opt.max_grad_norm
                            # if grad_norm > max_norm > 0:
                            #     clip_coef = max_norm / (grad_norm + 1e-6)
                            #     self.fp32_params.grad.data.mul_(clip_coef)

                            if self.opt.fp16:
                                grad_denom = 1
                            else:
                                grad_denom = normalizer
                            grad_norm = self.optim.step(grad_denom=grad_denom)  # update the parameters in fp32
                            self.meters['gnorm'].update(grad_norm)
                            # print("DEBUGGING 3")

                            if self.opt.fp16:
                                # copying the parameters back to fp16
                                offset = 0
                                for p in self.model.parameters():
                                    if not p.requires_grad:
                                        continue
                                    numel = p.data.numel()
                                    p.data.copy_(self.fp32_params.data[offset:offset + numel].view_as(p.data))
                                    offset += numel
                        except RuntimeError as e:
                            if 'out of memory' in str(e):
                                torch.cuda.empty_cache()
                                self.meters["oom"].update(1)
                            else:
                                raise e

                        self.model.zero_grad()
                        self.optim.zero_grad()
                        counter = 0
                        num_accumulated_words = 0
                        num_accumulated_sents = 0
                        num_updates = self.optim._step

                        if opt.save_every > 0 and num_updates % opt.save_every == -1 % opt.save_every:
                            valid_loss = self.eval(self.valid_data)
                            valid_ppl = math.exp(min(valid_loss, 100))
                            print('Validation perplexity: %g' % valid_ppl)

                            ep = float(epoch) - 1. + ((float(i) + 1.) / nSamples)

                            # self.save(ep, valid_ppl, batch_order=batch_order, iteration=i)

                num_words = tgt_size
                self.meters['report_loss'].update(loss_data)
                self.meters['report_tgt_words'].update(num_words)
                self.meters['report_src_words'].update(src_size)
                self.meters['total_loss'].update(loss_data)
                self.meters['total_words'].update(num_words)

                optim = self.optim

                if i == 0 or (i % opt.log_interval == -1 % opt.log_interval):
                    data_size = len(train_data)
                    self.logger.log(epoch, i, data_size)

                    # self.logger.reset_meter("report_loss")
                    # self.logger.reset_meter("report_tgt_words")
                    # self.logger.reset_meter("report_src_words")

        total_loss = self.meters['total_loss'].sum
        total_words = self.meters['total_words'].sum
        return total_loss / total_words

    def run(self, save_file=None):

        opt = self.opt
        model = self.model
        optim = self.optim
        self.model = self.model

        # Try to load the save_file
        checkpoint = None
        if save_file:
            checkpoint = torch.load(save_file)

        if opt.load_pretrained_nmt:
            print("Loading pretrained NMT from %s " % opt.load_pretrained_nmt)
            pretrained_cp = torch.load(opt.load_pretrained_nmt, map_location=lambda storage, loc: storage)

            transformer_model_weights = pretrained_cp['model']
            transformer_opt           = pretrained_cp['opt']

            self.nmt_model, _ = build_model(transformer_opt, self.dicts)

            self.nmt_model.load_state_dict(transformer_model_weights)

            if self.cuda:
                self.nmt_model = self.nmt_model.cuda()

            print("Done")
        else:
            raise NotImplementedError

        if checkpoint is not None:
            print('Loading model and optim from checkpoint at %s' % save_file)
            self.convert_fp16(checkpoint['model'], checkpoint['optim'])
            batch_order = checkpoint['batch_order']
            iteration = checkpoint['iteration'] + 1
            opt.start_epoch = int(math.floor(float(checkpoint['epoch'] + 1)))
            resume = True

            del checkpoint['model']
            del checkpoint['optim']
            del checkpoint

        else:
            batch_order = None
            iteration = 0
            print('Initializing model parameters')
            init_model_parameters(model, opt)
            resume = False

            if self.opt.fp16:
                self.convert_fp16()
            else:
                self.convert_fp32()



        valid_loss = self.eval(self.valid_data)
        valid_ppl = math.exp(min(valid_loss, 100))
        print('Validation perplexity: %g' % valid_ppl)

        self.start_time = time.time()

        for epoch in range(opt.start_epoch, opt.start_epoch + opt.epochs):
            print('')

            #  (1) train for one epoch on the training set
            train_loss = self.train_epoch(epoch, resume=resume,
                                          batch_order=batch_order,
                                          iteration=iteration)
            train_ppl = math.exp(min(train_loss, 100))
            print('Train perplexity: %g' % train_ppl)

            #  (2) evaluate on the validation set
            valid_loss = self.eval(self.valid_data)
            valid_ppl = math.exp(min(valid_loss, 100))
            print('Validation perplexity: %g' % valid_ppl)

            # only save at the end of epoch when the option to save "every iterations" is disabled
            # if self.opt.save_every <= 0:
                # self.save(epoch, valid_ppl)
            batch_order = None
            iteration = None
            resume = False





