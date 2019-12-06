"""
    Copyright 2019 Tae Hwan Jung
    ALBERT Implementation with forking
    Clean Pytorch Code from https://github.com/dhlee347/pytorchic-bert
"""

""" Training Config & Helper Classes  """

import os
import json
from typing import NamedTuple
from tqdm import tqdm

import torch
import torch.nn as nn

class Config(NamedTuple):
    """ Hyperparameters for training """
    seed: int = 3431 # random seed
    batch_size: int = 8
    lr: int = 5e-5 # learning rate
    n_epochs: int = 10 # the number of epoch
    # `warm up` period = warmup(0.1)*total_steps
    # linearly increasing learning rate from zero to the specified value(5e-5)
    warmup: float = 0.1
    save_steps: int = 100 # interval for saving model
    total_steps: int = 100000 # total number of steps to train

    @classmethod
    def from_json(cls, file): # load config from json file
        return cls(**json.load(open(file, "r")))

def generator_loss(model, batch, global_step, optimizer, cross_ent, sent_cross_ent, writer=None, prefix='pretrain'): # make sure loss is tensor
    input_ids, segment_ids, input_mask, masked_ids, masked_pos, masked_weights, is_next = batch
    logits_lm, logits_clsf = model(input_ids, segment_ids, input_mask, masked_pos)
    loss_lm = cross_ent(logits_lm.transpose(1, 2), masked_ids) # for masked LM
    loss_lm = (loss_lm*masked_weights.float()).mean()
    loss_sop = sent_cross_ent(logits_clsf, is_next) # for sentence classification

    if writer:
        writer.add_scalars(prefix+'/D',
                        {'loss_lm': loss_lm.item(),
                        'loss_sop': loss_sop.item(),
                        'loss_total': (loss_lm + loss_sop).item(),
                        'lr': optimizer.get_lr()[0],
                        },
                        global_step)
    return loss_lm + loss_sop, logits_lm, loss_sop


def discriminator_loss(model, batch, global_step, optimizer, cross_ent, sent_cross_ent, writer=None, prefix='pretrain'): # make sure loss is tensor
    input_ids, segment_ids, input_mask, masked_ids, masked_pos, masked_weights, is_next = batch
    logits_lm, logits_clsf = model(input_ids, segment_ids, input_mask, masked_pos)
    loss_lm = cross_ent(logits_lm.transpose(1, 2), masked_ids) # for masked LM
    loss_lm = (loss_lm*masked_weights.float()).mean()
    loss_sop = sent_cross_ent(logits_clsf, is_next) # for sentence classification

    if writer:
        writer.add_scalars(prefix+'/G',
                        {'loss_lm': loss_lm.item(),
                        'loss_sop': loss_sop.item(),
                        'loss_total': (loss_lm + loss_sop).item(),
                        'lr': optimizer.get_lr()[0],
                        },
                        global_step)
    return loss_lm + loss_sop, logits_lm, loss_sop


class Trainer(object):
    """Training Helper Class"""
    def __init__(self, cfg, model, data_iter, optimizer, save_dir, device):
        self.cfg = cfg # config for training : see class Config
        self.model = model
        self.data_iter = data_iter # iterator to load data
        self.optimizer = optimizer
        self.save_dir = save_dir
        self.device = device # device name

    def train(self, get_loss, model_file=None, data_parallel=False):
        """ Train Loop """
        if isinstance(self.model, tuple):
            for m in self.model:
                m.train() # train mode
        self.load(model_file)
        model = self.model.to(self.device)
        if data_parallel: # use Data Parallelism with Multi-GPU
            model = nn.DataParallel(model)

        global_step = 0 # global iteration steps regardless of epochs
        for e in range(self.cfg.n_epochs):
            loss_sum = 0. # the sum of iteration losses to get average loss in every epoch
            iter_bar = tqdm(self.data_iter, desc='Iter (loss=X.XXX)')
            for i, batch in enumerate(iter_bar):
                batch = [t.to(self.device) for t in batch]

                self.optimizer.zero_grad()
                loss, _, _ = discriminator_loss(model, batch, global_step, self.optimizer).mean() # mean() for Data Parallelism
                loss.backward()
                self.optimizer.step()

                global_step += 1
                loss_sum += loss.item()
                iter_bar.set_description('Iter (loss=%5.3f)'%loss.item())

                if global_step % self.cfg.save_steps == 0: # save
                    self.save(global_step)

                if self.cfg.total_steps and self.cfg.total_steps < global_step:
                    print('Epoch %d/%d : Average Loss %5.3f'%(e+1, self.cfg.n_epochs, loss_sum/(i+1)))
                    print('The Total Steps have been reached.')
                    self.save(global_step) # save and finish when global_steps reach total_steps
                    return

            print('Epoch %d/%d : Average Loss %5.3f'%(e+1, self.cfg.n_epochs, loss_sum/(i+1)))
        self.save(global_step)

    def eval(self, evaluate, model_file, data_parallel=True):
        """ Evaluation Loop """
        self.model.eval() # evaluation mode
        self.load(model_file)
        model = self.model.to(self.device)
        if data_parallel: # use Data Parallelism with Multi-GPU
            model = nn.DataParallel(model)

        results = [] # prediction results
        iter_bar = tqdm(self.data_iter, desc='Iter (loss=X.XXX)')
        for batch in iter_bar:
            batch = [t.to(self.device) for t in batch]
            with torch.no_grad(): # evaluation without gradient calculation
                accuracy, result = evaluate(model, batch) # accuracy to print
            results.append(result)

            iter_bar.set_description('Iter(acc=%5.3f)'%accuracy)
        return results

    def load(self, model_file):
        """ load saved model or pretrained transformer (a part of model) """
        if model_file:
            print('Loading the model from', model_file)
            self.model.load_state_dict(torch.load(model_file))

    def save(self, i):
        """ save current model """
        torch.save(self.model, os.path.join(self.save_dir, 'backbone.pt'))
        torch.save(self.model.state_dict(), # save model object before nn.DataParallel
            os.path.join(self.save_dir, 'model_steps_'+str(i)+'.pt'))


class AdversarialTrainer(object):
    """Training Helper Class"""
    def __init__(self, cfg, discriminator, generator, data_iter, d_optimizer, g_optimizer, save_dir, device):
        self.cfg = cfg # config for training : see class Config
        self.discriminator = discriminator
        self.generator = generator
        self.data_iter = data_iter # iterator to load data
        self.d_optimizer = d_optimizer
        self.g_optimizer = g_optimizer
        self.save_dir = save_dir
        self.device = device # device name
        self.cross_ent = nn.CrossEntropyLoss(reduction='none')
        self.sent_cross_ent = nn.CrossEntropyLoss()


    def train(self, model_file=None, data_parallel=False):
        """ Train Loop """
        self.discriminator.train() # train mode
        self.generator.train()
        self.load(model_file)
        generator = self.generator.to(self.device)
        discriminator = self.discriminator.to(self.device)
        if data_parallel: # use Data Parallelism with Multi-GPU
            discriminator = nn.DataParallel(discriminator)
            generator = nn.DataParallel(generator)

        global_step = 0 # global iteration steps regardless of epochs
        for e in range(self.cfg.n_epochs):
            loss_sum = 0. # the sum of iteration losses to get average loss in every epoch
            iter_bar = tqdm(self.data_iter, desc='Iter (loss=X.XXX)')
            for i, batch in enumerate(iter_bar):
                batch = [t.to(self.device) for t in batch]

                self.g_optimizer.zero_grad()
                g_loss, loss_lm, _ = generator_loss(generator, batch, global_step, 
                    self.g_optimizer,
                    self.cross_ent, self.sent_cross_ent,
                    self.writer, prefix='electra'
                    )
                g_loss.backward()
                if data_parallel:
                    g_loss.mean()
                self.g_optimizer.step()

                global_step += 1
                loss_sum += g_loss.item()

                loss_lm.detach()
                self.d_optimizer.zero_grad()     
                masked_ids = batch[3]
                loss_lm = torch.argmax(loss_lm, axis=2)
                is_replaced = (loss_lm == masked_ids).long()
                is_replaced = is_replaced.to(self.device)
                batch[3] = is_replaced

                d_loss, _, _ = discriminator_loss(discriminator, batch, global_step, 
                    self.d_optimizer,
                    self.cross_ent, self.sent_cross_ent,
                    self.writer, prefix='electra')
                if data_parallel:
                    d_loss.mean()

                self.d_optimizer.step()

                iter_bar.set_description('Iter (d_loss=%5.3f,' % d_loss.item(), 'g_loss=%5.3f' % g_loss.item())

                if global_step % self.cfg.save_steps == 0: # save
                    self.save(global_step)

                if self.cfg.total_steps and self.cfg.total_steps < global_step:
                    print('Epoch %d/%d : Average Loss %5.3f'%(e+1, self.cfg.n_epochs, loss_sum/(i+1)))
                    print('The Total Steps have been reached.')
                    self.save(global_step) # save and finish when global_steps reach total_steps
                    return

            print('Epoch %d/%d : Average Loss %5.3f'%(e+1, self.cfg.n_epochs, loss_sum/(i+1)))
        self.save(global_step)

    def eval(self, evaluate, model_file, data_parallel=True):
        """ Evaluation Loop """
        self.model.eval() # evaluation mode
        self.load(model_file)
        model = self.model.to(self.device)
        if data_parallel: # use Data Parallelism with Multi-GPU
            model = nn.DataParallel(model)

        results = [] # prediction results
        iter_bar = tqdm(self.data_iter, desc='Iter (loss=X.XXX)')
        for batch in iter_bar:
            batch = [t.to(self.device) for t in batch]
            with torch.no_grad(): # evaluation without gradient calculation
                accuracy, result = evaluate(model, batch) # accuracy to print
            results.append(result)

            iter_bar.set_description('Iter(acc=%5.3f)'%accuracy)
        return results

    def load(self, model_file):
        """ load saved model or pretrained transformer (a part of model) """
        if model_file:
            print('Loading the model from', model_file)
            self.model.load_state_dict(torch.load(model_file))

    def save(self, i):
        """ save current model """
        torch.save(self.model, os.path.join(self.save_dir, 'backbone.pt'))
        torch.save(self.model.state_dict(), # save model object before nn.DataParallel
            os.path.join(self.save_dir, 'model_steps_'+str(i)+'.pt'))
