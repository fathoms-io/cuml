
# Copyright (c) 2019, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import pytest

import rmm

from cuml.test.utils import array_equal, unit_param, quality_param, \
    stress_param
from cuml.neighbors import NearestNeighbors as cuKNN
from cuml.neighbors import kneighbors_graph as knn_graph_instance

from sklearn.neighbors import NearestNeighbors as skKNN
from sklearn.datasets.samples_generator import make_blobs
from sklearn.exceptions import NotFittedError
import cupy as cp
import cudf
import pandas as pd
import numpy as np

import sklearn
import cuml
from cuml.common import has_scipy


def predict(neigh_ind, _y, n_neighbors):
    import scipy.stats as stats

    neigh_ind = neigh_ind.astype(np.int32)

    ypred, count = stats.mode(_y[neigh_ind], axis=1)
    return ypred.ravel(), count.ravel() * 1.0 / n_neighbors


def valid_metrics():
    cuml_metrics = cuml.neighbors.VALID_METRICS["brute"]
    sklearn_metrics = sklearn.neighbors.VALID_METRICS["brute"]
    return [value for value in cuml_metrics if value in sklearn_metrics]


@pytest.mark.parametrize("datatype", ["dataframe", "numpy"])
@pytest.mark.parametrize("nrows", [500, 1000, 10000])
@pytest.mark.parametrize("ncols", [100, 1000])
@pytest.mark.parametrize("n_neighbors", [10, 50])
@pytest.mark.parametrize("n_clusters", [2, 10])
def test_neighborhood_predictions(nrows, ncols, n_neighbors, n_clusters,
                                  datatype):
    if not has_scipy():
        pytest.skip('Skipping test_neighborhood_predictions because ' +
                    'Scipy is missing')

    X, y = make_blobs(n_samples=nrows, centers=n_clusters,
                      n_features=ncols, random_state=0)

    X = X.astype(np.float32)

    if datatype == "dataframe":
        X = cudf.DataFrame.from_gpu_matrix(rmm.to_device(X))

    knn_cu = cuKNN()
    knn_cu.fit(X)
    neigh_ind = knn_cu.kneighbors(X, n_neighbors=n_neighbors,
                                  return_distance=False)

    if datatype == "dataframe":
        assert isinstance(neigh_ind, cudf.DataFrame)
        neigh_ind = neigh_ind.as_gpu_matrix().copy_to_host()
    else:
        assert isinstance(neigh_ind, np.ndarray)

    labels, probs = predict(neigh_ind, y, n_neighbors)

    assert array_equal(labels, y)


def test_return_dists():
    n_samples = 50
    n_feats = 50
    k = 5

    X, y = make_blobs(n_samples=n_samples,
                      n_features=n_feats, random_state=0)

    knn_cu = cuKNN()
    knn_cu.fit(X)

    ret = knn_cu.kneighbors(X, k, return_distance=False)
    assert not isinstance(ret, tuple)
    assert ret.shape == (n_samples, k)

    ret = knn_cu.kneighbors(X, k, return_distance=True)
    assert isinstance(ret, tuple)
    assert len(ret) == 2


@pytest.mark.parametrize('input_type', ['dataframe', 'ndarray'])
@pytest.mark.parametrize('nrows', [unit_param(500), quality_param(5000),
                         stress_param(500000)])
@pytest.mark.parametrize('n_feats', [unit_param(3), quality_param(100),
                         stress_param(1000)])
@pytest.mark.parametrize('k', [unit_param(3), quality_param(30),
                         stress_param(50)])
@pytest.mark.parametrize("metric", valid_metrics())
@pytest.mark.parametrize("mode", ['connectivity', 'distance'])
def test_cuml_against_sklearn(input_type, nrows, n_feats, k, metric, mode):
    X, _ = make_blobs(n_samples=nrows,
                      n_features=n_feats, random_state=0)

    p = 5  # Testing 5-norm of the minkowski metric only
    
    knn_sk = skKNN(metric=metric, p=p)  # Testing
    knn_sk.fit(X)
    D_sk, I_sk = knn_sk.kneighbors(X, k)
    CSR_sk = knn_sk.kneighbors_graph(X=X, mode=mode)

    X_orig = X

    if input_type == "dataframe":
        X = cudf.DataFrame.from_gpu_matrix(rmm.to_device(X))

    knn_cu = cuKNN(metric=metric, p=p)
    knn_cu.fit(X)
    D_cuml, I_cuml = knn_cu.kneighbors(X, k)
    CSR_cu = knn_cu.kneighbors_graph(X=X, mode=mode)

    cp.testing.assert_array_almost_equal(
        CSR_sk.toarray(), 
        CSR_cu.toarray(), 
        decimal=4)

    if input_type == "dataframe":
        assert isinstance(D_cuml, cudf.DataFrame)
        assert isinstance(I_cuml, cudf.DataFrame)
        D_cuml_arr = D_cuml.as_gpu_matrix().copy_to_host()
        I_cuml_arr = I_cuml.as_gpu_matrix().copy_to_host()
    else:
        assert isinstance(D_cuml, np.ndarray)
        assert isinstance(I_cuml, np.ndarray)
        D_cuml_arr = D_cuml
        I_cuml_arr = I_cuml

    # Assert the cuml model was properly reverted
    np.testing.assert_allclose(knn_cu.X_m.to_output("numpy"), X_orig,
                               atol=1e-5, rtol=1e-4)

    # Allow a max relative diff of 10% and absolute diff of 1%
    np.testing.assert_allclose(D_cuml_arr, D_sk, atol=1e-2,
                               rtol=1e-1)
    assert I_cuml_arr.all() == I_sk.all()

def test_knn_fit_twice():
    """
    Test that fitting a model twice does not fail.
    This is necessary since the NearestNeighbors class
    needs to free Cython allocated heap memory when
    fit() is called more than once.
    """

    n_samples = 1000
    n_feats = 50
    k = 5

    X, y = make_blobs(n_samples=n_samples,
                      n_features=n_feats, random_state=0)

    knn_cu = cuKNN()
    knn_cu.fit(X)
    knn_cu.fit(X)

    knn_cu.kneighbors(X, k)

    del knn_cu


@pytest.mark.parametrize('input_type', ['ndarray'])
@pytest.mark.parametrize('nrows', [unit_param(500), quality_param(5000),
                         stress_param(500000)])
@pytest.mark.parametrize('n_feats', [unit_param(20), quality_param(100),
                         stress_param(1000)])
def test_nn_downcast_fails(input_type, nrows, n_feats):
    X, y = make_blobs(n_samples=nrows,
                      n_features=n_feats, random_state=0)

    knn_cu = cuKNN()
    if input_type == 'dataframe':
        X_pd = pd.DataFrame({'fea%d' % i: X[0:, i] for i in range(X.shape[1])})
        X_cudf = cudf.DataFrame.from_pandas(X_pd)
        knn_cu.fit(X_cudf, convert_dtype=True)

    with pytest.raises(Exception):
        knn_cu.fit(X, convert_dtype=False)

    # Test fit() fails when downcast corrupted data
    X = np.array([[np.finfo(np.float32).max]], dtype=np.float64)
    knn_cu = cuKNN()
    with pytest.raises(Exception):
        knn_cu.fit(X, convert_dtype=False)





# https://github.com/scikit-learn/scikit-learn/blob/62fc8bb94dcd65e72878c0599ff91391d9983424/sklearn/neighbors/tests/test_neighbors.py#L1029-L1066
def test_kneighbors_graph_old():
    # Test kneighbors_graph to build the k-Nearest Neighbor graph.
    X = np.array([[0, 1], [1.01, 1.], [2, 0]])

    # n_neighbors = 1
    A = knn_graph_instance(X, 1, mode='connectivity',
                                   include_self=False)
    cp.testing.assert_array_almost_equal(A.toarray(), cp.eye(A.shape[0]))

    # A = knn_graph_instance(X, 2, mode='connectivity',
    #                                include_self=False)
    # cp.testing.assert_array_almost_equal(
    #     A.toarray(), 
    #     [[0., 1., 1.],
    #      [1., 0., 1.],
    #      [1., 1., 0.]])
         
    # A = knn_graph_instance(X, 2, mode='distance')
    # cp.testing.assert_array_almost_equal(
    #     A.toarray(),
    #     [[0., 1.01, 2.23606798],
    #      [1.01, 0., 1.40716026],
    #      [2.23606798, 1.40716026, 0.]])

    # n_neighbors = 3
    A = knn_graph_instance(X, 3, mode='connectivity', include_self=True)
    cp.testing.assert_array_almost_equal(
        A.toarray(),
        [[1, 1, 1], [1, 1, 1], [1, 1, 1]])

    # n_neighbors = 2
    A = knn_graph_instance(X, 2, mode='connectivity',
                                   include_self=True)
    cp.testing.assert_array_equal(
        A.toarray(),
        [[1., 1., 0.],
         [1., 1., 0.],
         [0., 1., 1.]])

    # A = knn_graph_instance(X, 1, mode='distance')
    # cp.testing.assert_array_almost_equal(
    #     A.toarray(),
    #     [[0.00, 1.01, 0.],
    #      [1.01, 0., 0.],
    #      [0.00, 1.40716026, 0.]])


def test_kneighbors_graph_compare():
    # Test kneighbors_graph to build the k-Nearest Neighbor graph.
    X = np.array([[0, 1], [1.01, 1.], [2, 0]])

    knn_sk = skKNN(n_neighbors=2, metric='minkowski', p=2,
                             metric_params=None, n_jobs=None).fit(X)
    sk_csr = knn_sk.kneighbors_graph(X=None, n_neighbors=2, mode='connectivity')
    indices, distances = knn_sk.kneighbors(X=None, n_neighbors=2)
    # distances = np.ones(indices.shape[0] * 2)
    print(indices)
    print(distances)
    # print(sk_csr.toarray())

    knn_cu = cuKNN(n_neighbors=2, metric='minkowski', p=2,
                             metric_params=None).fit(X)
    cu_csr = knn_cu.kneighbors_graph(X=None, n_neighbors=2, mode='connectivity')
    indices, distances = knn_cu.kneighbors(X=None, n_neighbors=2)
    # distances = cp.ones(indices.shape[0] * 2)
    print(indices)
    print(distances)
    # print(cu_csr.toarray())

    cp.testing.assert_array_almost_equal(
            sk_csr.toarray(), 
            cu_csr.toarray())


