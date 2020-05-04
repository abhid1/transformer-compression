import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchtext import data, datasets
import time, sys
import nmt.transformer as transformer
import nltk
import os
import sacrebleu


class Batch:
    "Object for holding a batch of data with mask during training."

    def __init__(self, src, trg=None, pad=0):
        self.src = src
        self.src_mask = (src != pad).unsqueeze(-2)
        if trg is not None:
            self.trg = trg[:, :-1]
            self.trg_y = trg[:, 1:]
            self.trg_mask = \
                self.make_std_mask(self.trg, pad)
            self.ntokens = (self.trg_y != pad).sum().item()

    @staticmethod
    def make_std_mask(tgt, pad):
        "Create a mask to hide padding and future words."
        tgt_mask = (tgt != pad).unsqueeze(-2)
        tgt_mask = tgt_mask & transformer.subsequent_mask(tgt.size(-1)).type_as(tgt_mask)
        return tgt_mask
    

class LossCompute:
    "Wrapper for simple loss compute and train function."

    def __init__(self, generator, criterion, opt=None):
        self.generator = generator
        self.criterion = criterion
        self.opt = opt

    def __call__(self, x, y, norm):
        x = self.generator(x)
        loss = self.criterion(x.contiguous().view(-1, x.size(-1)),
                              y.contiguous().view(-1)) / norm
        loss.backward()
        if self.opt is not None:
            self.opt.step()
            self.opt.optimizer.zero_grad()
        return loss.item() * norm


def lookup_words(x, vocab=None):
    if vocab is not None:
        x = [vocab.itos[i] for i in x]

    return [str(t) for t in x]


def evaluate_bleu(predictions, labels):
    try:
        #bleu_nltk = nltk.translate.bleu_score.corpus_bleu(labels, predictions)
        bleu_sacre = sacrebleu.raw_corpus_bleu(predictions, [labels], .01).score
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:
        print("\nWARNING: Could not compute BLEU-score. Error:", str(e))
        bleu_nltk = 0

    #return bleu_nltk
    return bleu_sacre
    
    
def valid(model, SRC, TGT, valid_iter, num_steps, to_words=False):
    translate = []
    tgt = []
    for i, batch in enumerate(valid_iter):

        src = batch.src.transpose(0, 1)[:1]
        src_mask = (src != SRC.vocab.stoi["<pad>"]).unsqueeze(-2)
        out = greedy_decode(model, src, src_mask,
                            max_len=50, start_symbol=TGT.vocab.stoi["<s>"])
        translate_str = []
        for j in range(1, out.size(1)):
            if to_words:
                sym = TGT.vocab.itos[out[0, j]]
                if sym == "</s>": break
            else:
                sym = out[0, j].item()
                if TGT.vocab.stoi["</s>"] == sym:
                    break
            translate_str.append(sym)
        tgt_str = []
        for j in range(1, batch.trg.size(0)):
            if to_words:
                sym = TGT.vocab.itos[batch.trg[j, 0]]
                if sym == "</s>": break
            else:
                sym = batch.trg[j, 0].item()
                if TGT.vocab.stoi["</s>"] == sym:
                    break
            tgt_str.append(sym)

        # else:
        #     translate_str = [str(i.item()) for i in out[0]]
        #     tgt_str = list(batch.trg[:, 0].cpu().numpy().astype(str))

        translate.append(translate_str)
        tgt.append([tgt_str])

        if (i + 1) % num_steps == 0:
            break

    translation_sentences = []
    target_sentences = []

    for translation_sentence_index in range(len(translate)):
        translation_sentences.append([TGT.vocab.itos[i] for i in translate[translation_sentence_index]])

    for target_sentence_index in range(len(tgt)):
        target_sentences.append([TGT.vocab.itos[i] for i in tgt[target_sentence_index][0]])

    # Essential for sacrebleu calculations
    translation_sentences = [" ".join(x) for x in translation_sentences]
    target_sentences = [" ".join(x) for x in target_sentences]

    print(translate[0], translation_sentences[0])
    print(tgt[0], target_sentences[0])

    print(translate[5], translation_sentences[5])
    print(tgt[5], target_sentences[5])

    # return evaluate_bleu(translate, tgt)
    return evaluate_bleu(translation_sentences, target_sentences)


def run_epoch(args, data_iter, model, loss_compute, valid_params=None, epoch_num=0,
            is_valid=False, is_test=False, logger=None):
    "Standard Training and Logging Function"
    start = time.time()
    total_tokens = 0
    total_loss = 0
    tokens = 0
    if valid_params is not None:
        src_dict, tgt_dict, valid_iter = valid_params
        hist_valid_scores = []

    bleu_all = 0
    count_all = 0

    for i, batch in enumerate(data_iter):
        model.train()
        out = model.forward(batch.src, batch.trg,
                            batch.src_mask, batch.trg_mask)

        loss = loss_compute(out, batch.trg_y, batch.ntokens)
        total_loss += loss
        total_tokens += batch.ntokens
        tokens += batch.ntokens

        if i % 51 == 0 and not is_valid:
            elapsed = time.time() - start
            print("Epoch Step: %d Loss: %f" %
                  (i, loss / float(batch.ntokens)))
            logger['loss'].append(total_loss / total_tokens)

            start = time.time()
            tokens = 0
           
        if (i + 1) % args.valid_every == 0 and valid_params is not None and not is_valid:
            model.eval()
            if args.multi_gpu:
                bleu_val = valid(model.module, src_dict, tgt_dict, valid_iter, args.valid_max_num)
            else:
                bleu_val = valid(model, src_dict, tgt_dict, valid_iter, args.valid_max_num)

            if logger is not None:
                logger['bleu'].append(bleu_val)

            print("BLEU ", bleu_val)

            is_better_than_last = len(hist_valid_scores) == 0 or bleu_val > sorted(hist_valid_scores)[-1]
            hist_valid_scores.append(bleu_val)

            if is_better_than_last:
                model_state_dict = model.state_dict()
                model_file = args.save_to + args.exp_name + '.best.bin'

                checkpoint = {
                    'model': model_state_dict,
                    'opts': loss_compute.opt,
                    'epoch': epoch_num
                }

                print('save model to [%s]' % model_file, file=sys.stderr)

                torch.save(checkpoint, model_file)

        if i % args.save_model_after == 0 and not is_valid and not args.save_best:
            model_state_dict = model.state_dict()
            model_file = args.save_to + args.exp_name +'.iter{}.epoch{}.bin'.format(i, epoch_num)

            checkpoint = {
                'model': model_state_dict,
                'opts': loss_compute.opt,
                'epoch': epoch_num
            }

            print('save model to [%s]' % model_file, file=sys.stderr)


            torch.save(checkpoint,model_file)

            print("")
            
    if is_valid:
        if args.multi_gpu:
            bleu_val = valid(model.module, src_dict, tgt_dict, valid_iter, 10000)
        else:
            bleu_val = valid(model, src_dict, tgt_dict, valid_iter, 10000)

        print("BLEU (validation) ", bleu_val)
        return total_loss / total_tokens, bleu_val

    if is_test:
        os.makedirs(args.save_to_file, exist_ok=True)
        if args.multi_gpu:
            bleu_val = test_decode(model.module, src_dict, tgt_dict, valid_iter, 10000, \
                                   to_words=True,
                                   file_path=os.path.join(args.save_to_file,args.exp_name))
        else:
            bleu_val = test_decode(model, src_dict, tgt_dict, valid_iter, 10000)

        print("BLEU (validation) ", bleu_val)
        return total_loss / total_tokens, bleu_val

    return total_loss / total_tokens, logger


class LabelSmoothing(nn.Module):
    "Implement label smoothing."

    def __init__(self, size, padding_idx, smoothing=0.0):
        super(LabelSmoothing, self).__init__()
        self.criterion = nn.KLDivLoss(size_average=False)
        self.padding_idx = padding_idx
        self.confidence = 1.0 - smoothing
        self.smoothing = smoothing
        self.size = size
        self.true_dist = None

    def forward(self, x, target):
        assert x.size(1) == self.size
        true_dist = torch.tensor(x, requires_grad=False)
        true_dist.fill_(self.smoothing / (self.size - 2))
        true_dist.scatter_(1, target.unsqueeze(1), self.confidence)
        true_dist[:, self.padding_idx] = 0
        mask = torch.nonzero(target == self.padding_idx)
        if len(mask) > 0:
            true_dist.index_fill_(0, mask.squeeze(), 0.0)
        self.true_dist = true_dist
        return self.criterion(x, torch.tensor(true_dist))

    
def rebatch(pad_idx, batch):
    "Fix order in torchtext"
    src, trg = batch.src.transpose(0, 1), batch.trg.transpose(0, 1)
    return Batch(src, trg, pad_idx)


def greedy_decode(model, src, src_mask, max_len, start_symbol):
    memory = model.encode(src, src_mask)
    batch_size = src.shape[0]
    ys = torch.ones(batch_size, 1).fill_(start_symbol).type_as(src)
    for i in range(max_len-1):
        out = model.decode(memory, src_mask,
                           ys,
                           transformer.subsequent_mask(ys.size(1))
                                    .type_as(src))
        prob = model.generator(out[:, -1])
        _, next_word = torch.max(prob, dim = 1)
        next_word = next_word.unsqueeze(1)
        ys = torch.cat([ys,
                        next_word.type_as(src)], dim=1)
    return ys


def test_decode(model, SRC, TGT, valid_iter, num_steps, to_words=False, file_path=None):
    translate = []
    tgt = []
    for i, batch in enumerate(valid_iter):

        src = batch.src.transpose(0, 1)

        src_mask = (src != SRC.vocab.stoi["<pad>"]).unsqueeze(-2)
        out = greedy_decode(model, src, src_mask,
                            max_len=50, start_symbol=TGT.vocab.stoi["<s>"])
        for k in range(out.size(0)):
            translate_str = []
            for j in range(1, out.size(1)):
                if to_words:
                    sym = TGT.vocab.itos[out[0, j]]
                    if sym == "</s>": break
                else:
                    sym = out[0, j].item()
                    if TGT.vocab.stoi["</s>"] == sym:
                        break
                translate_str.append(sym)
            tgt_str = []
            for j in range(1, batch.trg.size(0)):
                if to_words:
                    sym = TGT.vocab.itos[batch.trg[j, 0]]
                    if sym == "</s>": break
                else:
                    sym = batch.trg[j, 0].item()
                    if TGT.vocab.stoi["</s>"] == sym:
                        break
                tgt_str.append(sym)

            translate.append(translate_str)
            tgt.append([tgt_str])

    # Essential for sacrebleu calculations
    translation_sentences = [" ".join(x) for x in translate]
    target_sentences = [" ".join(x) for x in tgt[0]]

    print(translate[0], translation_sentences[0])
    print(tgt[0], target_sentences[0])

    print(translate[5], translation_sentences[5])
    print(tgt[5], target_sentences[5])

    if file_path is not None:
        print(len(translate))
        print(len(tgt))

        with open(file_path, 'w') as f:
            for hyps in translate:
                f.write(' '.join(hyps[1:-1]) + '\n')

        with open(file_path+'.targets', 'w') as f:
            for hyps in tgt:
                f.write(' '.join(hyps[0][1:-1]) + '\n')

    #return evaluate_bleu(translate, tgt)
    return evaluate_bleu(translation_sentences, target_sentences)
