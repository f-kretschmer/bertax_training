import os
os.environ['TF_KERAS']="1"
from tensorflow import keras
import keras_bert
import tensorflow as tf
from preprocessing.process_inputs import seq2kmers, ALPHABET
from random import randint
import numpy as np
from itertools import product
from logging import info, warning
from misc.metrics import compute_roc, accuracy, loss


# NOTE: uses just keras (instead of tensorflow.keras). Otherwise
# loading of the pre-trained model is likely to fail


def get_token_dict(alph=ALPHABET, k=3):
    """get token dictionary dict generated from `alph` and `k`"""
    token_dict = keras_bert.get_base_dict()
    for word in [''.join(_) for _ in product(alph, repeat=k)]:
        token_dict[word] = len(token_dict)
    return token_dict


def load_bert(bert_path, compile_=False):
    """get bert model from path"""
    custom_objects = {'GlorotNormal': keras.initializers.glorot_normal,
                      'GlorotUniform': keras.initializers.glorot_uniform}
    custom_objects.update(keras_bert.get_custom_objects())
    model = keras.models.load_model(bert_path, compile=compile_,
                                    custom_objects=custom_objects)
    return model


def generate_bert_with_pretrained(pretrained_path, nr_classes=4):
    """get model ready for fine-tuning and the maximum input length"""
    # see https://colab.research.google.com/github/CyberZHG/keras-bert
    # /blob/master/demo/tune/keras_bert_classification_tpu.ipynb
    model = load_bert(pretrained_path)
    inputs = model.inputs[:2]
    nsp_dense_layer = model.get_layer(name='NSP-Dense').output
    softmax_layer = keras.layers.Dense(nr_classes, activation='softmax')(
        nsp_dense_layer)
    model_fine = keras.Model(inputs=inputs, outputs=softmax_layer)
    return model_fine


def generate_bert_with_pretrained_multi_tax(pretrained_path, nr_classes=(4, 30, 100),
                                            tax_ranks=["superkingdom", "phylum", "family"]):
    """get model ready for fine-tuning and the maximum input length"""
    # see https://colab.research.google.com/github/CyberZHG/keras-bert
    # /blob/master/demo/tune/keras_bert_classification_tpu.ipynb
    custom_objects = {'GlorotNormal': keras.initializers.glorot_normal,
                      'GlorotUniform': keras.initializers.glorot_uniform}
    custom_objects.update(keras_bert.get_custom_objects())
    model = tf.keras.models.load_model(pretrained_path, compile=False,
                                    custom_objects=custom_objects)
    inputs = model.inputs[:2]
    nsp_dense_layer = model.get_layer(name='NSP-Dense').output

    # out_layer = []
    # previous_taxa = [nsp_dense_layer]
    # for index, nr_classes_tax_i in enumerate(nr_classes):
    #     if index != 0:
    #         tax_i_in = tf.keras.layers.concatenate(previous_taxa)
    #     else:
    #         tax_i_in = nsp_dense_layer
    #     tax_i_out = tf.keras.layers.Dense(nr_classes_tax_i,name=f"{tax_ranks[index]}_out", activation='softmax')(tax_i_in)
    #     previous_taxa.append(tax_i_out)
    #     out_layer.append(tax_i_out)

    tax_i_in = nsp_dense_layer
    out_layer = []
    for index, nr_classes_tax_i in enumerate(nr_classes):
        tax_i_out = tf.keras.layers.Dense(nr_classes_tax_i, name=f"{tax_ranks[index]}_out", activation='softmax',)(
            tax_i_in)
        out_layer.append(tax_i_out)
        tax_i_in_help = out_layer.copy()
        tax_i_in_help.append(nsp_dense_layer)
        tax_i_in = tf.keras.layers.concatenate(tax_i_in_help)

    # out_layer = []
    # previous_taxa = [nsp_dense_layer]
    # tax_i_in = nsp_dense_layer
    # tax_i_out = tf.keras.layers.Dense(nr_classes[0], activation='softmax',name="superkingdoms_softmax")(tax_i_in)
    # previous_taxa.append(tax_i_out)
    # out_layer.append(tax_i_out)
    #
    # tax_i_in = tf.keras.layers.concatenate(previous_taxa)
    # tax_i_out = tf.keras.layers.Dense(nr_classes[1],activation='softmax',name="families_softmax")(tax_i_in)
    # out_layer.append(tax_i_out)
    # model_fine = tf.keras.Model(inputs=inputs, outputs=tax_i_out)
    model_fine = tf.keras.Model(inputs=inputs, outputs=out_layer)
    return model_fine


def seq2tokens(seq, token_dict, seq_length=250, max_length=None,
               k=3, stride=3, window=True, seq_len_like=None):
    """transforms raw sequence into list of tokens to be used for
    fine-tuning BERT
    NOTE: intended to be used as `custom_encode_sequence` argument for
    DataGenerators"""
    if (max_length is None):
        max_length = seq_length
    if (seq_len_like is not None):
        seq_length = min(max_length, np.random.choice(seq_len_like))
        # open('seq_lens.txt', 'a').write(str(seq_length) + ', ')
    seq = seq2kmers(seq, k=k, stride=stride, pad=True)
    if (window):
        start = randint(0, max(len(seq) - seq_length - 1, 0))
        end = start + seq_length - 1
    else:
        start = 0
        end = seq_length
    indices = [token_dict['[CLS]']] + [token_dict[word]
                                       if word in token_dict
                                       else token_dict['[UNK]']
                                       for word in seq[start:end]]
    if (len(indices) < max_length):
        indices += [token_dict['']] * (max_length - len(indices))
    else:
        indices = indices[:max_length]
    segments = [0 for _ in range(max_length)]
    return [np.array(indices), np.array(segments)]


def process_bert_tokens_batch(batch_x):
    """when `seq2tokens` is used as `custom_encode_sequence`, batches
    are generated as [[input1, input2], [input1, input2], ...]. In
    order to train, they have to be transformed to [input1s,
    input2s] with this function"""
    return [np.array([_[0] for _ in batch_x]),
            np.array([_[1] for _ in batch_x])]


def predict(model, test_generator, roc_auc=True, classes=None,
            return_data=False, store_x=False, nonverbose=False, calc_metrics=True):
    from preprocessing.generate_data import PredictGenerator # prevent cyclic import
    predict_g = PredictGenerator(test_generator, store_x=store_x)
    preds = model.predict(predict_g, verbose=0 if nonverbose else 1)

    if len(predict_g.get_targets()[0].shape) >= 2:  # in case a single model has multiple outputs
        y = [np.array(target[:len(preds[0])]) for target in predict_g.get_targets()]  # in case not everything was predicted
    else:
        y = predict_g.get_targets()[:len(preds)]  # in case not everything was predicted

    if (len(y) > 0 and calc_metrics):
        acc = accuracy(y, preds)
        result = [acc]
        metrics_names = ['test_accuracy']
        if model._is_compiled:
            loss_ = loss(y, preds)
            result.append(loss_)
            metrics_names.append('test_loss')
        if roc_auc:
            roc_auc = compute_roc(y, preds, classes).roc_auc
            result.append(roc_auc)
            metrics_names.append('roc_auc')
    else:
        result = []
        metrics_names = []
    return {'metrics': result, 'metrics_names': metrics_names,
            'data': (y, preds) if return_data else None,
            'x': predict_g.get_x() if return_data and store_x else None}


def get_classes_and_weights_multi_tax(species_list, classes_preset=None, tax_ranks=['superkingdom', 'kingdom', 'family'],
                                      unknown_thr=10_000, norm_weights=True):
    from utils.tax_entry import TaxidLineage
    tlineage = TaxidLineage()
    num_entries = len(species_list)

    # reduce number of tax_ids to query
    unq, unq_idx, unq_cnt = np.unique(species_list, return_inverse=True, return_counts=True)
    species_list = zip(unq,unq_cnt)

    classes = dict()
    weight_classes = dict()
    tax_ranks_dict = dict()
    species_list_y = []
    for tax_rank_i in tax_ranks:
        tax_ranks_dict.update({tax_rank_i: dict()})

    for taxid, count in species_list:
        ranks = tlineage.get_ranks(taxid, ranks=tax_ranks)
        taxid_y = []
        for tax_rank_i in tax_ranks:
            num_same_tax_rank_i = tax_ranks_dict[tax_rank_i].get(ranks[tax_rank_i][1], 0) + count
            tax_ranks_dict[tax_rank_i].update({ranks[tax_rank_i][1]: num_same_tax_rank_i})
            taxid_y.append(ranks[tax_rank_i][1])
        species_list_y.append(taxid_y)

    for index, tax_rank_i in enumerate(tax_ranks_dict.keys()):
        dict_ = tax_ranks_dict[tax_rank_i]
        classes_tax_i = dict_.copy()
        unknown = 0
        weight_classes_tax_i = dict()
        for key, value in dict_.items():
            if classes_preset is not None:
                if key not in classes_preset[tax_rank_i]:
                    unknown += value
                    classes_tax_i.pop(key)
                else:
                    weight = num_entries / value
                    weight_classes_tax_i.update({key: weight})
            else:
                if value < unknown_thr:
                    unknown += value
                    classes_tax_i.pop(key)
                else:
                    weight = num_entries / value
                    weight_classes_tax_i.update({key: weight})

        unknown += classes_tax_i.get("unknown", 0)
        # if unknown != 0:
        classes_tax_i.update({'unknown': unknown})
        classes.update({tax_ranks[index]: classes_tax_i})

        # if unknown != 0:
        weight = num_entries / unknown if unknown != 0 else 1
        weight_classes_tax_i.update({'unknown': weight})

        # normalize weights see https://scikit-learn.org/stable/modules/generated/sklearn.utils.class_weight.compute_class_weight.html
        if norm_weights:
            num_classes = len(weight_classes_tax_i)
            weight_classes_tax_i = {key: value / num_classes for key, value in weight_classes_tax_i.items()}
        # update global dict with all weights for tax rank i
        weight_classes.update({tax_ranks[index]: weight_classes_tax_i})

    species_list_y = np.array(species_list_y)
    species_list_y = np.array([i if i in classes[tax_ranks[j]] else 'unknown' for j in range(len(tax_ranks)) for i in
                               species_list_y[:, j]]).reshape((len(tax_ranks), -1)).swapaxes(0, 1)
    # after reduction of querys species_list is in different order the unq_idx mask reverses this
    species_list_y = species_list_y[unq_idx]

    return classes, weight_classes, species_list_y
