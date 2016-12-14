# -*- coding: utf-8 -*-

import numpy as np
import copy
import math
import logging
import sys
import time
from keras_wrapper.dataset import Data_Batch_Generator, Homogeneous_Data_Batch_Generator



class BeamSearchEnsemble():

    def __init__(self, models, dataset, params_prediction):
        """

        :param models:
        :param dataset:
        :param params_prediction:
        """
        self.models = models
        self.dataset = dataset
        self.params = params_prediction

    # ------------------------------------------------------- #
    #       PREDICTION FUNCTIONS
    #           Functions for making prediction on input samples
    # ------------------------------------------------------- #

    def predict_cond(self, models, X, states_below, params, ii, optimized_search=False, prev_outs=None):

        probs_list = []
        prev_outs_list = []
        for i, model in enumerate(models):
            if optimized_search:
                [model_probs, next_outs] = model.predict_cond(X, states_below, params, ii,
                                                              optimized_search=True, prev_out=prev_outs[i])
                probs_list.append(model_probs)
                prev_outs_list.append(next_outs)
            else:
                probs_list.append(model.predict_cond(X, states_below, params, ii))

        probs = sum(probs_list[i] for i in xrange(len(models))) / float(len(models))

        if optimized_search:
            return probs, prev_outs_list
        else:
            return probs

    def beam_search(self, X, params, null_sym=2):
        """
        Beam search method for Cond models.
        (https://en.wikibooks.org/wiki/Artificial_Intelligence/Search/Heuristic_search/Beam_search)
        The algorithm in a nutshell does the following:

        1. k = beam_size
        2. open_nodes = [[]] * k
        3. while k > 0:

            3.1. Given the inputs, get (log) probabilities for the outputs.

            3.2. Expand each open node with all possible output.

            3.3. Prune and keep the k best nodes.

            3.4. If a sample has reached the <eos> symbol:

                3.4.1. Mark it as final sample.

                3.4.2. k -= 1

            3.5. Build new inputs (state_below) and go to 1.

        4. return final_samples, final_scores

        :param X: Model inputs
        :param params: Search parameters
        :param null_sym: <null> symbol
        :return: UNSORTED list of [k_best_samples, k_best_scores] (k: beam size)
        """
        k = params['beam_size'] + 1
        samples = []
        sample_scores = []
        pad_on_batch = params['pad_on_batch']
        dead_k = 0  # samples that reached eos
        live_k = 1  # samples that did not yet reached eos
        hyp_samples = [[]] * live_k
        hyp_scores  = np.zeros(live_k).astype('float32')

        # we must include an additional dimension if the input for each timestep are all the generated "words_so_far"
        if params['words_so_far']:
            if k > params['maxlen']:
                raise NotImplementedError("BEAM_SIZE can't be higher than MAX_OUTPUT_TEXT_LEN on the current implementation.")
            state_below = np.asarray([[null_sym]] * live_k) if pad_on_batch else np.asarray([np.zeros((params['maxlen'], params['maxlen']))] * live_k)
        else:
            state_below = np.asarray([null_sym] * live_k) if pad_on_batch else np.asarray([np.zeros(params['maxlen'])] * live_k)

        prev_outs = None
        for ii in xrange(params['maxlen']):
            # for every possible live sample calc prob for every possible label
            if params['optimized_search']: # use optimized search model if available
                [probs, prev_outs] = self.predict_cond(self.models, X, state_below, params, ii,
                                                       optimized_search=True, prev_outs=prev_outs)
            else:
                probs = self.predict_cond(self.models, X, state_below, params, ii)
            # total score for every sample is sum of -log of word prb
            cand_scores = np.array(hyp_scores)[:, None] - np.log(probs)
            cand_flat = cand_scores.flatten()
            # Find the best options by calling argsort of flatten array
            ranks_flat = cand_flat.argsort()[:(k-dead_k)]

            # Decypher flatten indices
            voc_size = probs.shape[1]
            trans_indices = ranks_flat / voc_size # index of row
            word_indices = ranks_flat % voc_size # index of col
            costs = cand_flat[ranks_flat]

            # Form a beam for the next iteration
            new_hyp_samples = []
            new_hyp_scores = np.zeros(k-dead_k).astype('float32')
            for idx, [ti, wi] in enumerate(zip(trans_indices, word_indices)):
                new_hyp_samples.append(hyp_samples[ti]+[wi])
                new_hyp_scores[idx] = copy.copy(costs[idx])

            # check the finished samples
            new_live_k = 0
            hyp_samples = []
            hyp_scores = []
            indices_alive = []
            for idx in xrange(len(new_hyp_samples)):
                if new_hyp_samples[idx][-1] == 0:  # finished sample
                    samples.append(new_hyp_samples[idx])
                    sample_scores.append(new_hyp_scores[idx])
                    dead_k += 1
                else:
                    indices_alive.append(idx)
                    new_live_k += 1
                    hyp_samples.append(new_hyp_samples[idx])
                    hyp_scores.append(new_hyp_scores[idx])
            hyp_scores = np.array(hyp_scores)
            live_k = new_live_k

            if new_live_k < 1:
                break
            if dead_k >= k:
                break
            state_below = np.asarray(hyp_samples, dtype='int64')

            # we must include an additional dimension if the input for each timestep are all the generated words so far
            if pad_on_batch:
                state_below = np.hstack((np.zeros((state_below.shape[0], 1), dtype='int64') + null_sym, state_below))
                if params['words_so_far']:
                    state_below = np.expand_dims(state_below, axis=0)
            else:
                state_below = np.hstack((np.zeros((state_below.shape[0], 1), dtype='int64'), state_below,
                                         np.zeros((state_below.shape[0], max(params['maxlen'] - state_below.shape[1] - 1, 0)),
                                         dtype='int64')))

                if params['words_so_far']:
                    state_below = np.expand_dims(state_below, axis=0)
                    state_below = np.hstack((state_below,
                                             np.zeros((state_below.shape[0], params['maxlen'] - state_below.shape[1], state_below.shape[2]))))

            if params['optimized_search'] and ii > 0:
                i = 0
                for prev_out in prev_outs:
                    # filter next search inputs w.r.t. remaining samples
                    for idx_vars in range(len(prev_out)):
                        prev_outs[i][idx_vars] = prev_out[idx_vars][indices_alive]
                    i += 1

        # dump every remaining one
        if live_k > 0:
            for idx in xrange(live_k):
                samples.append(hyp_samples[idx])
                sample_scores.append(hyp_scores[idx])

        return samples, sample_scores


    def BeamSearchNet(self):
        """
        Approximates by beam search the best predictions of the net on the dataset splits chosen.

        :param batch_size: size of the batch
        :param n_parallel_loaders: number of parallel data batch loaders
        :param normalization: apply data normalization on images/features or not (only if using images/features as input)
        :param mean_substraction: apply mean data normalization on images or not (only if using images as input)
        :param predict_on_sets: list of set splits for which we want to extract the predictions ['train', 'val', 'test']
        :param optimized_search: boolean indicating if the used model has the optimized Beam Search implemented (separate self.model_init and self.model_next models for reusing the information from previous timesteps).

        The following attributes must be inserted to the model when building an optimized search model:

            * ids_inputs_init: list of input variables to model_init (must match inputs to conventional model)
            * ids_outputs_init: list of output variables of model_init (model probs must be the first output)
            * ids_inputs_next: list of input variables to model_next (previous word must be the first input)
            * ids_outputs_next: list of output variables of model_next (model probs must be the first output and the number of out variables must match the number of in variables)
            * matchings_init_to_next: dictionary from 'ids_outputs_init' to 'ids_inputs_next'
            * matchings_next_to_next: dictionary from 'ids_outputs_next' to 'ids_inputs_next'

        :returns predictions: dictionary with set splits as keys and matrices of predictions as values.
        """

        # Check input parameters and recover default values if needed
        default_params = {'batch_size': 50, 'n_parallel_loaders': 8, 'beam_size': 5,
                          'normalize': False, 'mean_substraction': True,
                          'predict_on_sets': ['val'], 'maxlen': 20, 'n_samples':-1,
                          'model_inputs': ['source_text', 'state_below'],
                          'model_outputs': ['description'],
                          'dataset_inputs': ['source_text', 'state_below'],
                          'dataset_outputs': ['description'],
                          'alpha_factor': 1.0,
                          'sampling_type': 'max_likelihood',
                          'words_so_far': False,
                          'optimized_search': False
                          }
        params = self.checkParameters(self.params, default_params)

        # Check if the model is ready for applying an optimized search
        if params['optimized_search']:
            if 'matchings_init_to_next' not in dir(self) or \
                            'matchings_next_to_next' not in dir(self) or \
                            'ids_inputs_init' not in dir(self) or \
                            'ids_outputs_init' not in dir(self) or \
                            'ids_inputs_next' not in dir(self) or \
                            'ids_outputs_next' not in dir(self):
                raise Exception(
                    "The following attributes must be inserted to the model when building an optimized search model:\n",
                    "- matchings_init_to_next\n",
                    "- matchings_next_to_next\n",
                    "- ids_inputs_init\n",
                    "- ids_outputs_init\n",
                    "- ids_inputs_next\n",
                    "- ids_outputs_next\n")

        predictions = dict()
        for s in params['predict_on_sets']:
            logging.info("<<< Predicting outputs of "+s+" set >>>")
            assert len(params['model_inputs']) > 0, 'We need at least one input!'
            params['pad_on_batch'] = self.dataset.pad_on_batch[params['dataset_inputs'][-1]]
            # Calculate how many interations are we going to perform
            if params['n_samples'] < 1:
                n_samples = eval("self.dataset.len_"+s)
                num_iterations = int(math.ceil(float(n_samples)/params['batch_size']))

                # Prepare data generator: We won't use an Homogeneous_Data_Batch_Generator here
                #TODO: We prepare data as model 0... Different data preparators for each model?
                data_gen = Data_Batch_Generator(s, self.models[0], self.dataset, num_iterations,
                                         batch_size=params['batch_size'],
                                         normalization=params['normalize'],
                                         data_augmentation=False,
                                         mean_substraction=params['mean_substraction'],
                                         predict=True).generator()
            else:
                n_samples = params['n_samples']
                num_iterations = int(math.ceil(float(n_samples)/params['batch_size']))

                # Prepare data generator: We won't use an Homogeneous_Data_Batch_Generator here
                data_gen = Data_Batch_Generator(s, self.models[0], self.dataset, num_iterations,
                                         batch_size=params['batch_size'],
                                         normalization=params['normalize'],
                                         data_augmentation=False,
                                         mean_substraction=params['mean_substraction'],
                                         predict=False,
                                         random_samples=n_samples).generator()
            if params['n_samples'] > 0:
                references = []
                sources = []
            out = []
            total_cost = 0
            sampled = 0
            start_time = time.time()
            eta = -1
            for j in range(num_iterations):
                data = data_gen.next()
                X = dict()
                if params['n_samples'] > 0:
                    s_dict = {}
                    for input_id in params['model_inputs']:
                        X[input_id] = data[0][input_id]
                        s_dict[input_id] = X[input_id]
                    sources.append(s_dict)

                    Y = dict()
                    for output_id in params['model_outputs']:
                        Y[output_id] = data[1][output_id]
                else:
                    for input_id in params['model_inputs']:
                        X[input_id] = data[input_id]

                for i in range(len(X[params['model_inputs'][0]])):
                    sampled += 1
                    sys.stdout.write('\r')
                    sys.stdout.write("Sampling %d/%d  -  ETA: %ds " % (sampled, n_samples, int(eta)))
                    sys.stdout.flush()
                    x = dict()
                    for input_id in params['model_inputs']:
                        x[input_id] = np.asarray([X[input_id][i]])
                    samples, scores = self.beam_search(x, params, null_sym=self.dataset.extra_words['<null>'])
                    if params['normalize']:
                        counts = [len(sample)**params['alpha_factor'] for sample in samples]
                        scores = [co / cn for co, cn in zip(scores, counts)]
                    best_score = np.argmin(scores)
                    best_sample = samples[best_score]
                    out.append(best_sample)
                    total_cost += scores[best_score]
                    eta = (n_samples - sampled) *  (time.time() - start_time) / sampled
                    if params['n_samples'] > 0:
                        for output_id in params['model_outputs']:
                            references.append(Y[output_id][i])

            sys.stdout.write('Total cost of the translations: %f \t Average cost of the translations: %f\n'%(total_cost, total_cost/n_samples))
            sys.stdout.write('The sampling took: %f secs (Speed: %f sec/sample)\n' % ((time.time() - start_time), (
            time.time() - start_time) / n_samples))

            sys.stdout.flush()

            predictions[s] = np.asarray(out)

        if params['n_samples'] < 1:
            return predictions
        else:
            return predictions, references, sources


    def checkParameters(self, input_params, default_params):
        """
            Validates a set of input parameters and uses the default ones if not specified.
        """
        valid_params = [key for key in default_params]
        params = dict()

        # Check input parameters' validity
        for key,val in input_params.iteritems():
            if key in valid_params:
                params[key] = val
            else:
                raise Exception("Parameter '"+ key +"' is not a valid parameter.")

        # Use default parameters if not provided
        for key,default_val in default_params.iteritems():
            if key not in params:
                params[key] = default_val

        return params
