from sklearn.model_selection import StratifiedKFold, LeaveOneGroupOut, KFold
from sklearn.model_selection import GridSearchCV, RandomizedSearchCV
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.preprocessing import OrdinalEncoder

import random
import anndata
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


from functools import partial
import os
import pkgutil
import importlib
from utilities.logger import Logger
from utilities.plot import plot
import logging
import json

from config import cfg
from models import flatModels, globalModels
from metrics.calibration_error import calibration_error
from scipy.special import softmax
from calibration.calibrate_model import CalibratedClassifier
# import models

from sklearn.metrics import accuracy_score, f1_score, balanced_accuracy_score
from statistics import mean

# TODO: refactor to dataloader module
# TODO: Enable pass dataset matrix  to app 
# TODO: check if train and test have the sampe y.nunique()
# TODO: outer metrics, rejection option , update res dict
# TODO: documentation
# TODO: dump Results to disk every three(interval) classifiers in case of training failure

# create logger
logger = Logger(name='App', log_to_file=True, log_to_console=False)

class Benchmark:
    def __init__(self, classifiers, datasets, tuning_mode='sample'):
        self.__validate_input(tuning_mode)
        self.tuning_mode = tuning_mode
        self.classifiers = classifiers
        self.results = None
        self.datasets = datasets
        self.task_name = None


    def __validate_input(self,tuning_mode):
        tuning_mode_category = ['full', 'sample']
        if tuning_mode.lower() not in tuning_mode_category:
            raise ValueError(f'Available modes are: {", ".join(tuning_mode_category)}')


    def __train(self, inner_cv, inner_metrics, outer_metrics, outer_cv=None, dataset=None, pre_splits=None):
        true_labels_test = []
        res = {}
        for classifier in self.classifiers:
            logger.write(f'{classifier.name}:', msg_type='subtitle')
            best_params = []
            model_result = {}
            params_search_required = True
            pipeline_steps=[]

            if pre_splits is None:
                X, y, groups = dataset
                n_splits = outer_cv.get_n_splits(X, y, groups) #TODO: take care of cv method except LeaveOneGroupOut
                splits = outer_cv.split(X, y, groups)
            else:
                n_splits = len(pre_splits)
                splits = pre_splits

            # Nested CV: Perform grid search with outer(model selection) and inner(parameter tuning) cross-validation
            for fold_idx, (train, test) in enumerate(splits):
                
                if pre_splits is None:
                    X_train, X_test = X[train], X[test]
                    y_train, y_test = y[train], y[test]
                else:
                    X_train, X_test = train[0], test[0]
                    y_train, y_test = train[1], test[1]

                # Initialise model
                pipeline, param_grid, y_train, y_test = classifier.init_model(X_train, y_train, y_test)

                # Hold-out validation set for calibration
                # train_idx, val_idx_cal = next(inner_cv.split(X_train, y_train)) # TODO: change
                train_idx, val_idx_cal = next(inner_cv.split(X_train)) # TODO: change
                X_train, X_val_cal = X_train[train_idx], X_train[val_idx_cal]
                y_train, y_val_cal = y_train[train_idx], y_train[val_idx_cal]

                if len(true_labels_test) < n_splits:
                    true_labels_test.append(y_test.tolist())

                # Fine-tuned model 
                if not param_grid:
                    model_selected = pipeline
                    params_search_required = False
                # Tune Params
                else:
                    if self.tuning_mode.lower() == 'sample':
                        model_selected = RandomizedSearchCV(pipeline, param_grid, cv=inner_cv, scoring=inner_metrics, n_iter=1 if cfg.debug else 30, refit=True, n_jobs=-1) # For debug
                    else:
                        model_selected = GridSearchCV(pipeline, param_grid, cv=inner_cv, scoring=inner_metrics, refit=True, n_jobs=-1)
                model_selected.fit(X_train, y_train)
                
                if params_search_required:
                    best_params.append(model_selected.best_params_)
                    if fold_idx == n_splits - 1:
                        pipeline_steps = model_selected.best_estimator_.get_params()["steps"]
                else:
                    if fold_idx == n_splits - 1:
                        # best_params = None
                        if hasattr(model_selected, 'get_params'):
                            pipeline_steps = model_selected.get_params()['steps']

                
                # for param, value in  model_selected.best_estimator_.get_params().items():
                #     print(f"{param}: {value}")
                # Log results in the last fold
                if fold_idx == n_splits - 1: 
                    if not params_search_required:
                        logger.write(
                            (f'Steps in pipline: {dict(pipeline_steps)}\n' # TODO: case: no pipeline 
                            f'Best hyperparameters: Not available, parameters are defined by user.'), 
                            msg_type='content'
                        )
                    else:
                        best_params_unique = [str(dict(y)) for y in set(tuple(x.items()) for x in best_params)]
                        logger.write(
                            (f'Steps in pipline: {dict(pipeline_steps)}\n'
                            f'Best hyperparameters ({len(best_params_unique)}/{n_splits}): {", ".join(best_params_unique)}'),
                            msg_type='content'
                        )
                y_test_predict_uncalib = model_selected.predict(X_test)
                classifier.set_modelFitted(model_selected)
                # Uncaliberated confidence
                y_test_proba_uncalib_all, logits = classifier.predict_proba(model_selected, X_test)
                y_test_proba_uncalib_all = y_test_proba_uncalib_all.astype(float)

                # Caliberation: skip Probabilistic model
                y_test_proba_calib = []
                y_test_proba_uncalib = []
                y_test_predict_calib = []
                if logits is not None:
                    # model_calibrated = CalibratedClassifierCV(model_selected, cv='prefit', method="sigmoid", n_jobs=-1)
                    model_calibrated = CalibratedClassifier(classifier)
                    model_calibrated.fit(X_val_cal, y_val_cal)

                    y_test_predict_calib = model_calibrated.predict(X_test)
                    y_test_proba_calib_all = model_calibrated.predict_proba(X_test).astype(float)
                    for sample_idx, class_idx in enumerate(y_test_predict_calib):
                        y_test_proba_calib.append(y_test_proba_calib_all[sample_idx, class_idx])
                        if y_test_proba_uncalib_all is not None:
                            y_test_proba_uncalib.append(y_test_proba_uncalib_all[sample_idx, class_idx])
                    y_test_predict_calib = y_test_predict_calib.tolist()
                    print(calibration_error(y_test, y_test_predict_calib, y_test_proba_calib))
                else: #TODO reafactor to func
                    for sample_idx, class_idx in enumerate(y_test_predict_uncalib):
                        if y_test_proba_uncalib_all is not None:
                            y_test_proba_uncalib.append(y_test_proba_uncalib_all[sample_idx, class_idx])
                    # y_test_predict_calib = y_test_predict_calib.tolist()
                    print(calibration_error(y_test, y_test_predict_uncalib, y_test_proba_uncalib))

                model_result.setdefault('predicts_calib', []).append(y_test_predict_calib) 
                model_result.setdefault('predicts_uncalib', []).append(y_test_predict_uncalib.tolist()) 
                model_result.setdefault('proba_calib', []).append(y_test_proba_calib) 
                model_result.setdefault('proba_uncalib', []).append(y_test_proba_uncalib) 

                y_test_predict = y_test_predict_uncalib 

                # Calculate metrics
                for metric_name, metric in outer_metrics.items():
                    score = metric(y_test_predict, y_test)
                    scores = model_result.setdefault('scores', {})
                    scores.setdefault(f'{metric_name}', {}).setdefault('full', []).append(score)

                    if fold_idx == n_splits - 1:
                        scores_ = scores[metric_name]['full']
                        mean_score = np.mean(scores_) 
                        median_score = np.median(scores_)
                        scores[metric_name].update({'mean': mean_score, 'median': median_score})

                        logger.write(
                            f'{metric_name.upper()}: Best Mean Score: {mean_score:.4f}  Best Median Score: {median_score:.4f}',
                            msg_type='content'
                        )
                        res[classifier.name] = model_result
                # break
            
            logger.write('', msg_type='content')

        return res, true_labels_test
    

    def save(self, dir):
        if not os.path.exists(dir):
            os.makedirs(dir)

        with open(os.path.join(dir, self.task_name), 'w') as file:
            json.dump(self.results, file, indent=3, separators=(', ', ': '))
    
    
    def plot(self, dir, metric_name='f1_score_macro'):
        plot(self.results, metric_name, os.path.join(dir, self.task_name))


    def run(self, inner_metrics, outer_metrics, task_name = 'testing_run', random_seed=15, description='', is_pre_splits=True, is_outer_cv=False):
        self.task_name = task_name
        logger.write(f'Task {task_name.upper()} Started.', msg_type='title')

        self.results = dict(random_state=random_seed, description=description)
        # Loop over different datasets
        # for name, path in data_paths.items():
        for name, dataset in self.datasets.items():
            logger.write(f'Start benchmarking models on dataset {name.upper()}.', msg_type='subtitle')
            # inner_cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=random_seed)
            inner_cv = KFold(n_splits=5, shuffle=True, random_state=random_seed)
            outer_cv = LeaveOneGroupOut()
            if not is_pre_splits:
                model_results, true_labels_test = self.__train(inner_cv, outer_cv, inner_metrics, outer_metrics, dataset=dataset)
            else:
                model_results, true_labels_test = self.__train(inner_cv, inner_metrics, outer_metrics, pre_splits=dataset)

            self.results.setdefault('datasets', {}).update({name: {'model_results': model_results, 'true_labels_test': true_labels_test}})
            
        # self._save() # dump the predicts

        logger.write(
            '~~~TASK COMPLETED~~~\n\n',
            msg_type='subtitle'
        )
            

if __name__ == "__main__":
    # Get raw data path
    current_file_dir = os.path.dirname(__file__)
    data_dir = os.path.join(os.path.dirname(current_file_dir), 'data-raw')
    path_asap = data_dir + "/ASAP41_final.h5ad"
    path_bgee = data_dir + "/SRP200614.h5ad"

    # Load raw data
    # ann = anndata.read_h5ad(path_bgee)
    # # X = ann.X
    # # y = ann.obs['cellTypeId'].cat.codes
    # mask = ann.obs['cellTypeId'] != 'unannotated'
    # X = ann.X[mask][:100]
    # y = ann.obs[mask]['cellTypeId'].cat.codes[:100]
    
    # groups = None
    # if 'batch' in ann.obs.columns:
    #     groups = ann.obs['batch']
    # # Datatype for torch tensors
    # X, y = X.astype(float32), y.astype(np.int64)
    # bgee = (X.toarray(), y, groups)

    # Load embeddings
    path =  data_dir + '/processed'
    splits_fn = {}
    for fn in os.listdir(path):
        idx = fn.find('_train_')
        if idx == -1:
            idx = fn.find('_test_')
        if idx != -1:
            tissue_name = fn[:idx]
            splits_fn.setdefault(tissue_name, []).append(fn)

    splits_data = {}
    for k, v in splits_fn.items():
        v = sorted(v, key = lambda x : (x[x.rindex('_') + 1:], x[x.rindex('_') - 1]))
        # v = sorted(v, key = lambda x : (x[x.rindex('_') + 1], x[x.rindex('_') - 1]), reverse=True)
        splits_fn[k] = [(v[i],v[i + 1]) for i in range(0,len(v), 2)]
        data = []
        for i in range(0,len(v), 2):
            ann_train = anndata.read_h5ad(path + '/' + v[i])
            ann_test = anndata.read_h5ad(path + '/' + v[i + 1])
            splits_data.setdefault(k, []).append([(ann_train.X, ann_train.obs['y']), (ann_test.X, ann_test.obs['y'])])
    
    # Run App
    bm = Benchmark(tuning_mode="sample") 
    
    params = dict(
        # selected_models=['NeuralNet'], 
        selected_models=['C-HMCNN'], 
        # data_paths={'bgee': path_bgee, 'asap': path_asap},
        datasets = splits_data,
        inner_metrics='accuracy',
        outer_metrics={'accuracy': accuracy_score, 'balanced_accuracy_score': balanced_accuracy_score, 'f1_score_macro': partial(f1_score, average='macro'), 'f1_score_weighted': partial(f1_score, average='weighted')},
        # outer_metrics={'accuracy': accuracy_score, 'f1_score': f1_score },
        task_name='testing_run'
    )
    bm.run(**params)