import os
import jieba
import torch
import pickle
import pandas as pd
import torch.nn as nn

from ark_nlp.model.tc.bert import Bert
from ark_nlp.model.tc.bert import BertConfig
from ark_nlp.model.tc.bert import Dataset
from ark_nlp.model.tc.bert import Task
from ark_nlp.model.tc.bert import get_default_model_optimizer
from ark_nlp.model.tc.bert import Tokenizer

train_data_df = pd.read_csv(
    './train.txt', 
    sep='\t',
    header=None, 
    names=['text', 'normalized_result']
)

train_data_df['normalized_result_num'] = train_data_df['normalized_result'].apply(lambda x: len(x.split('##')))
train_data_df['normalized_result_num_label'] = train_data_df['normalized_result_num'].apply(lambda x: 0 if x > 2 else x)

train_data_df = (train_data_df
                 .loc[:,['text', 'normalized_result_num_label']]
                 .rename(columns={'normalized_result_num_label': 'label'}))

tc_dataset = Dataset(train_data_df)

tc_train_dataset = Dataset(train_data_df)
tc_dev_dataset = Dataset(train_data_df)

import transformers 
from transformers import AutoTokenizer

bert_vocab = transformers.AutoTokenizer.from_pretrained('nghuyong/ernie-1.0')

max_seq_length=100

tokenizer = Tokenizer(bert_vocab, max_seq_length)

tc_dataset.convert_to_ids(tokenizer)

torch.cuda.empty_cache()

from ark_nlp.model.tc.bert import Task
from ark_nlp.factory.loss_function.focal_loss import FocalLoss
from ark_nlp.factory.utils.attack import FGM
import time
import tqdm
import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import sklearn.metrics as sklearn_metrics

from tqdm import tqdm
from torch.optim import lr_scheduler
from torch.autograd import Variable
from torch.autograd import grad
from torch.utils.data import DataLoader
from torch.utils.data import Dataset

from ark_nlp.factory.loss_function import get_loss
from ark_nlp.factory.optimizer import get_optimizer

class AttackTask(Task):
    
    def _on_train_begin(
        self, 
        train_data, 
        validation_data, 
        batch_size,
        lr, 
        params, 
        shuffle,
        train_to_device_cols=None,
        **kwargs
    ):
        
        if self.class_num == None:
            self.class_num = train_data.class_num  
        
        if train_to_device_cols == None:
            self.train_to_device_cols = train_data.to_device_cols
        else:
            self.train_to_device_cols = train_to_device_cols

        train_generator = DataLoader(train_data, batch_size=batch_size, shuffle=True, collate_fn=self._collate_fn)
        self.train_generator_lenth = len(train_generator)
            
        self.optimizer = get_optimizer(self.optimizer, self.module, lr, params)
        self.optimizer.zero_grad()
        
        self.module.train()
        
        self.fgm = FGM(self.module)
        
        self._on_train_begin_record(**kwargs)
        
        return train_generator
    
    def _on_backward(
        self, 
        inputs, 
        logits, 
        loss, 
        gradient_accumulation_steps=1,
        grad_clip=None,
        **kwargs
    ):
                
        # 如果GPU数量大于1
        if self.n_gpu > 1:
            loss = loss.mean()
        # 如果使用了梯度累积，除以累积的轮数
        if gradient_accumulation_steps > 1:
            loss = loss / gradient_accumulation_steps
            
        loss.backward() 
        
        self.fgm.attack()
        logits = self.module(**inputs)
        attck_loss = self._get_train_loss(inputs, logits, **kwargs)
        attck_loss.backward()
        self.fgm.restore() 
        
        if grad_clip != None:
            torch.nn.utils.clip_grad_norm_(self.module.parameters(), grad_clip)
        
        self._on_backward_record(**kwargs)
        
        return loss
    
import gc
import copy
from transformers import BertConfig
from sklearn.model_selection import KFold

kf = KFold(10, shuffle=True, random_state=42)

examples = copy.deepcopy(tc_dataset.dataset)

for fold_, (train_ids, dev_ids) in enumerate(kf.split(examples)):

    tc_train_dataset.dataset = [examples[_idx] for _idx in train_ids]
    tc_dev_dataset.dataset = [examples[_idx] for _idx in dev_ids]

    bert_config = BertConfig.from_pretrained('nghuyong/ernie-1.0', 
                                             num_labels=len(tc_train_dataset.cat2id))

    dl_module = Bert.from_pretrained('nghuyong/ernie-1.0', 
                                            config=bert_config)

    param_optimizer = list(dl_module.named_parameters())
    param_optimizer = [n for n in param_optimizer if 'pooler' not in n[0]]
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
         'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]     

    model = AttackTask(dl_module, 'adamw', 'lsce', cuda_device=1, ema_decay=0.995)
    
    model.fit(tc_train_dataset, 
              tc_dev_dataset,
              lr=3e-5,
              epochs=5, 
              batch_size=32,
              params=optimizer_grouped_parameters,
              evaluate_save=True,
              save_module_path='./checkpoint/predict_num/' + str(fold_) + '.pth'
             )
    
    del dl_module
    del model
    
    gc.collect()
    
    torch.cuda.empty_cache()
    
import pickle
with open('./checkpoint/predict_num/cat2id1.pkl', "wb") as f:
    pickle.dump(tc_train_dataset.cat2id, f)
