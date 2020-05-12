#!/usr/bin/env bash

train_log="train."${job_name}".log"
model_path=$1
CUDA_VISIBLE_DEVICES='0,1' PYTHONPATH="." \
python3 nmt/train.py --seed 45 \
  --min_freq 1 \
  --valid_max_num 4 \
  --save_model 1000 \
  --batch_s 150 \
  --tok --lower \
  --save_model_after 200 \
  --max_ep 20 \
  --exp iwslt  \
  --iwslt \
  --save_best \
  --valid_every 100 \
  --multi-gpu\
  --mode test \
  --load_model ${model_path} \
  --num_bl 6