# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.
#

"""
Train a network on multiple GPUs using multiprocessing.
"""

from itertools import cycle, islice
import math
import torch
import logging
import sys

import onmt
from onmt.multiprocessing.multiprocessing_event_loop import MultiprocessingEventLoop, Future
import onmt.multiprocessing.nccl as nccl
from onmt.utils import torch_persistent_save
from torch.serialization import default_restore_location
from apex import amp


"""
An utility function to send the data to the GPU
"""


def prepare_sample(batch, device=None, fp16=False):

    # TODO: sample is a Batch object. This function probably
    batch = rewrap(batch)
    batch.cuda(fp16=fp16, device=device)
    # pass
    # for i, t in enumerate(sample):
    #     sample[i] = Variable(t.cuda(device=device))
    #
    return batch


def aggregate_loss(losses):
    
    return sum(losses)


def aggregate_logging_outputs(logging_outputs):
    
    output = {}
    
    output['src_size'] = 0
    output['tgt_size'] = 0
    
    for log in logging_outputs:
        if 'src_size' in log:
            output['src_size'] += log['src_size']
        if 'tgt_size' in log:
            output['tgt_size'] += log['tgt_size']
        
    return output


class MultiprocessingRunner(MultiprocessingEventLoop):
    """Main class for multi-GPU training.
    Each GPU has a full copy of the model and is assigned to its own Python
    process. Gradients are accumulated with all-reduce and all model replicas
    are updated synchronously after each batch.
    The methods in this class are divided into synchronous functions, which
    prepare and dispatch the input to each process, and asynchronous functions
    (prefixed with `_async_`), which run on each process in parallel.
    """
    
    def __init__(self, opt, model, loss_function, device_ids=None,
                 multiprocessing_method='spawn'):
                     
        if device_ids is None:
            device_ids = tuple(range(torch.cuda.device_count()))
            
        super().__init__(device_ids, multiprocessing_method)
    
        if not torch.cuda.is_available():
            raise NotImplementedError('Training on CPU is not supported')
            
        print("Initializing multi-gpu training with %d devices" % self.num_replicas)
            
        model = model.share_memory()    
        nccl_uid = nccl.get_unique_id()
        self.loss_function = loss_function
        
        Future.gen_list([
            self.call_async(rank, '_async_init', args=opt, model=model,
                            loss_function=loss_function, nccl_uid=nccl_uid, opt=opt)
            for rank in range(self.num_replicas)
        ])
        
        self._grads_initialized = False
        
        self.initialize_gradients()
        
        self.set_seed(opt.seed)
        
    def _async_init(self, rank, device_id, args, model, loss_function, nccl_uid):
        """Initialize child processes."""
        self.args = args
        self.rank = rank

        # set CUDA device
        torch.cuda.set_device(device_id)

        # initialize NCCL
        nccl.initialize(self.num_replicas, nccl_uid, device_id)

        # copy model and loss_function to current device
        self.model = model.cuda()
        self.loss_function = loss_function.cuda()

        # initialize optimizer and LR scheduler
        self.optim = self._build_optimizer()
        self.optim.set_parameters(self.model.parameters())

        opt = self.args
        if not opt.fp16:
            opt_level = "O0"
            keep_batchnorm_fp32 = False
        elif opt.fp16_mixed:
            opt_level = "O1"
            keep_batchnorm_fp32 = None
        else:
            opt_level = "O2"
            keep_batchnorm_fp32 = False

        self.model, self.optim.optimizer = amp.initialize(self.model,
                                                          self.optim.optimizer,
                                                          opt_level=opt_level,
                                                          keep_batchnorm_fp32=keep_batchnorm_fp32,
                                                          loss_scale="dynamic",
                                                          verbosity=1 if self.opt.verbose else 0)
        
        self.loss = None
        self._max_bsz_seen = 0    
    
        print("GPU %d initialized successfully" % device_id)
    
    def _build_optimizer(self):
        
        optimizer = onmt.Optim(self.args)
        
        return optimizer
    
    def get_model(self):
        """Get one of the model replicas."""
        # just return the first model, since all replicas are the same
        return self.call_async(0, '_async_get_model').gen()
        
    def get_optim(self):
        """Get one of the model replicas."""
        # just return the first model, since all replicas are the same
        return self.call_async(0, '_async_get_optim').gen()
        
    def _async_get_optim(self, rank, device_id):
        return self.optim
        
    def _async_get_model(self, rank, device_id):
        return self.model
        
    def state_dict(self):
        """Save a checkpoint for the current model."""
        return self.call_async(0, '_async_state_dict').gen()
    
    def save_checkpoint(self, checkpoint, filename):
        """Save a checkpoint for the current model."""
        self.call_async(0, '_async_save_checkpoint', checkpoint=checkpoint, filename=filename).gen()
    
    def _async_save_checkpoint(self, rank, device_id, checkpoint, filename):

        # TODO: write the correct save function
        model_state_dict = self.model.state_dict()
        optim_state_dict = self.optim.state_dict()

        checkpoint['model'] = model_state_dict
        checkpoint['optim'] = optim_state_dict
        
        torch_persistent_save(checkpoint, filename)
        
        return [0]
    
    def _async_state_dict(self, rank, device_id):
        
        model_state_dict = self.model.state_dict()
        optim_state_dict = self.optim.state_dict()
        
        return model_state_dict, optim_state_dict
        
    def load_checkpoint(self, filename):
        """Load a checkpoint into the model replicas in each process."""
        results = Future.gen_list([
            self.call_async(rank, '_async_load_checkpoint', filename=filename)
            for rank in range(self.num_replicas)
        ])
        
        return results[0]

    def _async_load_checkpoint(self, rank, device_id, filename):
        # todo: write the correct load function

        checkpoint = torch.load(
            filename,
            map_location=lambda s, l: default_restore_location(s, 'cuda:{}'.format(device_id))
        )
        
        try:
            self.model.load_state_dict(checkpoint['model'])
        except:
            raise Exception('Cannot load model parameters from checkpoint, '
                            'please ensure that the architectures match')
        
        try:
            self.optimizer.load_state_dict(checkpoint['optim'])
        except:
            raise Exception('Cannot load optimizer parameters for some reason.')    
        
        del checkpoint['model']
        del checkpoint['optim']    
            
        return checkpoint

    def set_seed(self, seed):
        Future.gen_list([
            self.call_async(rank, '_async_set_seed', seed=seed)
            for rank in range(self.num_replicas)
        ])

    def _async_set_seed(self, rank, device_id, seed):
        torch.manual_seed(seed)

    def _async_forward(self, rank, device_id, eval=False, backward=False):
        if eval:
            self.model.eval()
        else:
            self.model.train()

        opt = self.args

        if opt.streaming:
            if train_data.is_new_stream():
                streaming_state = self.model.init_stream()
        else:
            streaming_state = None

        logging_output, loss_data, oom = {}, 0, False
        logging_output['src_size'] = 0
        logging_output['tgt_size'] = 0
        if self._sample is not None:
            try:
                # calculate loss and sample size
                #~ self.loss, sample_size, logging_output = self.loss_function(self.model, self._sample)
                targets = batch.get('target_output')
                tgt_mask = targets.ne(onmt.constants.PAD)
                outputs = self.model(batch, streaming=opt.streaming, target_mask=tgt_mask,
                                     zero_encoder=opt.zero_encoder,
                                     mirror=opt.mirror_loss, streaming_state=streaming_state,
                                     nce=opt.nce)

                batch_size = batch.size

                loss_dict = self.loss_function(outputs, targets, model=self.model)
                loss_data = loss_dict['data']
                loss = loss_dict['loss']  # a little trick to avoid gradient overflow with fp16
                full_loss = loss

                if opt.mirror_loss:
                    rev_loss = loss_dict['rev_loss']
                    rev_loss_data = loss_dict['rev_loss_data']
                    mirror_loss = loss_dict['mirror_loss']
                    full_loss = full_loss + rev_loss + mirror_loss
                    mirror_loss_data = loss_dict['mirror_loss'].item()
                else:
                    rev_loss = None
                    rev_loss_data = None
                    mirror_loss_data = 0

                # reconstruction loss
                if opt.reconstruct:
                    rec_loss = loss_dict['rec_loss']
                    rec_loss = rec_loss
                    full_loss = full_loss + rec_loss
                    rec_loss_data = loss_dict['rec_loss_data']
                else:
                    rec_loss_data = None

                if opt.lfv_multilingual:
                    lid_logits = outputs['lid_logits']
                    lid_labels = batch.get('target_lang')
                    lid_loss_function = self.loss_function.get_loss_function('lid_loss')
                    lid_loss = lid_loss_function(lid_logits, lid_labels)
                    full_loss = full_loss + lid_loss

                grad_scaler = min(opt.batch_size_words, 16384) if opt.update_frequency > 1 else batch.tgt_size

                if backward:
                    optimizer = self.optim.optimizer
                    full_loss.div_(grad_scaler)
                    with amp.scale_loss(full_loss, optimizer) as scaled_loss:
                        scaled_loss.backward()
                    del full_loss

                self.loss = loss_data

                logging_output['src_size'] = batch.src_size
                logging_output['tgt_size'] = batch.tgt_size
                logging_output['rev_loss_data'] = rev_loss_data
                logging_output['mirror_loss_data'] = mirror_loss_data
                logging_output['rec_loss_data'] = rec_loss_data
                logging_output['loss'] = loss_data

            except RuntimeError as e:
                if not eval and 'out of memory' in str(e):
                    print('| WARNING: ran out of memory on GPU #{}, skipping batch'.format(device_id))
                    sys.stdout.flush()
                    oom = True
                    if hasattr(torch.cuda, 'empty_cache'):
                        torch.cuda.empty_cache()
                else:
                    raise e
                    
            self._sample = None

        return loss_data, logging_output, oom
        
    def update_parameters(self, grad_denom=1):

        """ When we update parameters, all replicas update at the same time"""
        Future.gen_tuple_list([
            self.call_async(rank, '_async_update', grad_denom=grad_denom)
            for rank in range(self.num_replicas)
        ])

    def check_global_overflow(self):

        local_over_flows = Future.gen_tuple_list([
            self.call_async(rank, '_async_local_overflow')
            for rank in range(self.num_replicas)
        ])

        global_flows = sum(local_over_flows)

        return global_flows > 0

    def _async_local_overflow(self):

        if not self.args.fp16:
            return 0

        local_overflow = 0
        if self.optim.optimizer._amp_stash.already_patched:
            local_overflow = 1

        return local_overflow

    def _async_update(self, rank, device_id, grad_denom, is_global_overflow):

        # is it possible to run out of memory in this case ? ...

        if is_global_overflow:
            def patch_step(opt):
                """this function is copied from apex"""
                opt_step = opt.step

                def skip_step(closure=None):
                    if closure is not None:
                        raise RuntimeError("Currently, Amp does not support closure use with optimizers.")
                    #logger.info(f"Device[{self.gpu_rank}] Gradient overflow. Skipping step. "
                    #            "(This is from hack-for-optimizer-sync)")
                    if hasattr(opt._amp_stash, "all_fp32_from_fp16_params"):
                        # Clear the master grads that wouldn't be zeroed by model.zero_grad()
                        for param in opt._amp_stash.all_fp32_from_fp16_params:
                            param.grad = None
                    if hasattr(opt, "most_recent_scale"):
                        opt.most_recent_scale = 1.0
                        opt.scale_set_by_backward = False
                    opt.step = opt_step
                    opt._amp_stash.already_patched = False

                return skip_step

            # since there is someone
            if not self.optim.optimizer._amp_stash.already_patched:
                patch_step(self.optim.optimizer)
        else:
            self._all_reduce_and_rescale_grads(grad_denom=grad_denom)
            self.optimizer.step()
        return [0]
        
    def zero_grad(self):
        
        Future.gen_tuple_list([
            self.call_async(rank, '_async_zero_grad')
            for rank in range(self.num_replicas)
        ])
        
    def _async_zero_grad(self, rank, device_id):
        
        self.optimizer.zero_grad()  
        return [0]
        
    def step(self, samples, eval=False):
        
        self._scatter_samples(samples,replace_empty_samples=False)
        
        # call the async forward function
        losses, logging_outputs, ooms = Future.gen_tuple_list([
            self.call_async(rank, '_async_forward', eval=eval)
            for rank in range(self.num_replicas)
        ])
        
        logging_output = aggregate_logging_outputs(logging_outputs)
        loss = aggregate_loss(losses)
        
        logging_output['oom'] = sum(ooms)
        logging_output['loss'] = loss
        
        return logging_output

    def create_iterator(self):

        """
        N processes create N data iterators. Each of them
        :return:
        """

        pass

    def _scatter_samples(self, batches, replace_empty_samples=False):
        """Split and distribute a sample across GPUs."""
        if not replace_empty_samples:
            # pad with None until its size is equal to the number of replicas
            batches = batches + [None]*(self.num_replicas - len(samples))
        else:
            # pad by cycling through the given samples
            batches = list(islice(cycle(batches), self.num_replicas))

        assert len(samples) == self.num_replicas

        Future.gen_list([
            self.call_async(rank, '_async_prepare_sample', batch=batches[rank])
            for rank in range(self.num_replicas)
        ])

    def _async_prepare_batch(self, rank, device_id, batch):
        if batch is None:
            self._sample = None
        else:
            self._batch = prepare_sample(batch, fp16=self.args.fp16 and not self.args.fp16_mixed, device=device_id)

            size = self._batch.src_size + self._batch.tgt_size
            if size > self._max_bsz_seen:
                self._max_bsz_seen = size
                torch.cuda.empty_cache()  # reset cache to avoid random out of memory

    def initialize_gradients(self):
    
        Future.gen_tuple_list([
            self.call_async(rank, '_async_initialize_gradients')
            for rank in range(self.num_replicas)
        ])
        self._grads_initialized = True
            
    def _async_initialize_gradients(self, rank, device_id):
        """
        Since Torch lazily initialize the gradients with None
        We need a dummy forward / backward pass to get all variables' gradients initialized
        """
        for p in self.model.parameters():
            # p.grad = Variable(p.data.new(*p.size()).zero_())
            if not hasattr(p.grad, 'data'):
                dummy_loss = 0
                for this_para in self.model.parameters():
                    dummy_loss += torch.mean(this_para)
                dummy_loss.backward()
                break
        self.model.zero_grad()    
        return [0]

    def _all_reduce_and_rescale_grads(self, grad_denom=1, buffer_size=1048576000):
        """All-reduce and rescale gradients in chunks of the specified size."""
        grads = [p.grad.data for p in self.model.parameters() if p.requires_grad]
        # sys.stdout.flush()
        buffer_t = grads[0].new(math.ceil(buffer_size / grads[0].element_size())).zero_()
        buffer = []

        def all_reduce_buffer():
            # copy grads into buffer_t
            offset = 0
            for g in buffer:
                numel = g.numel()
                buffer_t[offset:offset+numel].copy_(g.view(-1))
                offset += numel
            # all-reduce and rescale
            nccl.all_reduce(buffer_t[:offset])
            
            if grad_denom > 1:
                buffer_t.div_(grad_denom)
            # copy all-reduced buffer back into grads
            offset = 0
            for g in buffer:
                numel = g.numel()
                g.view(-1).copy_(buffer_t[offset:offset+numel])
                offset += numel

        filled = 0
        for g in grads:
            sz = g.numel() * g.element_size()
            if sz > buffer_size:
                # grad is bigger than buffer, all-reduce and rescale directly
                nccl.all_reduce(g)
                g.div_(grad_denom)
            elif filled + sz > buffer_size:
                # buffer is full, all-reduce and replace buffer with grad
                all_reduce_buffer()
                buffer = [g]
                filled = sz
            else:
                # add grad to buffer
                buffer.append(g)
                filled += sz
        if len(buffer) > 0:
            all_reduce_buffer()
        
    
