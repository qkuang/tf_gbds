"""
Base class for a generative model and linear dynamical system implementation.
Based on Evan Archer"s code here:
https://github.com/earcher/vilds/blob/master/code/RecognitionModel.py
"""

import tensorflow as tf
import numpy as np
import lib.blk_tridiag_chol_tools as blk
from edward.models import RandomVariable
from tensorflow.contrib.distributions import Distribution
from tensorflow.contrib.distributions import FULLY_REPARAMETERIZED


class SmoothingLDSTimeSeries(RandomVariable, Distribution):
    """A "smoothing" recognition model to approximate the posterior and create
    the appropriate sampling expression given some observations.
    Neural networks are used to parameterize mean and precision (approximated
    by tridiagonal matrix/tensor).

    x ~ N( mu(y), sigma(y) )

    """
    def __init__(self, params, Input, xDim, yDim, extra_conds, *args,
                 **kwargs):
        """Initialize SmoothingLDSTimeSeries random variable (batch)

        Args:
            params: A Dictionary.
                Dictionary of time series-specific parameters. Contents:
                    * A: linear dynamics matrix (eigeninitial_values with
                         magnitude strictly less than 1);
                    * QinvChol: square root of the innovation covariance
                                matrix Q inverse;
                    * Q0invChol: square root of the initial innovation
                                 covariance matrix Q0 inverse;
                    * Neural network parameters: NN_Mu, NN_Lambda, NN_LambdaX.
            Input: A Tensor. Observations based on which samples are drawn.
            xDim, yDim: Integers. Dimension of latent space (x) and
                        observation (y).
            name: Optional name for the random variable.
                  Default to "SmoothingLDSTimeSeries".
        """
        name = kwargs.get("name", "SmoothingLDSTimeSeries")
        with tf.name_scope(name):
            self.y_t = tf.identity(Input, "observations")
            self.dyn_params = params["dyn_params"]
            self.xDim = xDim
            self.yDim = yDim
            self.extra_dim = params["extra_dim"]
            with tf.name_scope("batch_size"):
                self.B = tf.shape(Input)[0]
            with tf.name_scope("trial_length"):
                self.Tt = tf.shape(Input)[1]

            with tf.name_scope("pad_extra_conds"):
                if extra_conds is not None:
                    self.extra_conds = tf.identity(
                        extra_conds, "extra_conditions")
                    self.y = tf.concat([self.y_t, self.extra_conds], -1)
                else:
                    self.y = self.y_t
                    self.extra_conds = None

            # self.trainable = trainable

            self.NN_Mu = params["NN_Mu"]
            # Mu will automatically be of size [Batch_size x T x xDim]
            self.Mu = tf.identity(self.NN_Mu(self.y), "Mu")
            # self.Mu = tf.identity(tf.cond(
            #     self.trainable, lambda: self.NN_Mu(self.y),
            #     lambda: tf.stop_gradient(self.NN_Mu(self.y))), "Mu")

            self.NN_Lambda = params["NN_Lambda"]
            self.NN_Lambda_output = tf.identity(
                self.NN_Lambda(self.y), "flattened_LambdaChol")
            # self.NN_Lambda_output = tf.identity(tf.cond(
            #     self.trainable, lambda: self.NN_Lambda(self.y),
            #     lambda: tf.stop_gradient(self.NN_Lambda(self.y))),
            #     "flattened_LambdaChol")
            self.LambdaChol = tf.reshape(
                self.NN_Lambda_output,
                [self.B, self.Tt, self.xDim, self.xDim], "LambdaChol")

            self.NN_LambdaX = params["NN_LambdaX"]
            self.NN_LambdaX_output = tf.identity(
                self.NN_LambdaX(self.y[:, 1:]), "flattened_LambdaXChol")
            # self.NN_LambdaX_output = tf.identity(tf.cond(
            #     self.trainable, lambda: self.NN_LambdaX(self.y[:, 1:]),
            #     lambda: tf.stop_gradient(self.NN_LambdaX(self.y[:, 1:]))),
            #     "flattened_LambdaXChol")
            self.LambdaXChol = tf.reshape(
                self.NN_LambdaX_output,
                [self.B, self.Tt - 1, self.xDim, self.xDim], "LambdaXChol")

            with tf.name_scope("init_posterior"):
                self._initialize_posterior_distribution(params)

            # var_list = (self.NN_Mu.variables + self.NN_Lambda.variables +
            #             self.NN_LambdaX.variables + [self.A] +
            #             [self.QinvChol] + [self.Q0invChol])
            # self.var_list = var_list
            # self.log_vars = var_list
            self.log_vars = params['trainable_variables']

        if "name" not in kwargs:
            kwargs["name"] = name
        if "dtype" not in kwargs:
            kwargs["dtype"] = tf.float32
        if "reparameterization_type" not in kwargs:
            kwargs["reparameterization_type"] = FULLY_REPARAMETERIZED
        if "validate_args" not in kwargs:
            kwargs["validate_args"] = True
        if "allow_nan_stats" not in kwargs:
            kwargs["allow_nan_stats"] = False

        super(SmoothingLDSTimeSeries, self).__init__(*args, **kwargs)
        self._args = (params, Input, xDim, yDim, extra_conds)

    def _initialize_posterior_distribution(self, params):
        # Compute the precisions (from square roots)
        self.Lambda = tf.matmul(self.LambdaChol, tf.transpose(
            self.LambdaChol, [0, 1, 3, 2]), name="Lambda")
        self.LambdaX = tf.matmul(self.LambdaXChol, tf.transpose(
            self.LambdaXChol, [0, 1, 3, 2]), name="LambdaX")

        # Create dynamics matrix & initialize innovations precision,
        # [Batch_size x xDim x xDim]
        with tf.name_scope("dynamics_matrix"):
            self.A = self.dyn_params["A"]

        with tf.name_scope("initialize_innovations_precision"):
            self.QinvChol = self.dyn_params["QinvChol"]
            self.Qinv = tf.matmul(self.QinvChol, tf.transpose(
                self.QinvChol), name="Qinv")

            self.Q0invChol = self.dyn_params["Q0invChol"]
            self.Q0inv = tf.matmul(self.Q0invChol, tf.transpose(
                self.Q0invChol), name="Q0inv")

        with tf.name_scope("noise_penalty"):
            if "p" in params:
                self.p = tf.constant(params["p"], tf.float32, name="p")
            else:
                self.p = None

        # Put together the total precision matrix
        with tf.name_scope("precision_matrix"):
            AQinvA = tf.matmul(tf.matmul(tf.transpose(self.A), self.Qinv),
                               self.A, name="AQinvA")
            AQinvrep = tf.tile(tf.expand_dims(
                -tf.matmul(tf.transpose(self.A), self.Qinv, name="AQinv"), 0),
                [self.Tt - 1, 1, 1], "AQinvrep")
            AQinvArep = tf.tile(tf.expand_dims(AQinvA + self.Qinv, 0),
                                [self.Tt - 2, 1, 1], "AQinvArep")
            AQinvArepPlusQ = tf.concat(
                [tf.expand_dims(self.Q0inv + AQinvA, 0), AQinvArep,
                 tf.expand_dims(self.Qinv, 0)], 0, "AQinvArepPlusQ")

            with tf.name_scope("diagonal_blocks"):
                self.AA = tf.add(self.Lambda + tf.pad(
                    self.LambdaX, [[0, 0], [1, 0], [0, 0], [0, 0]]),
                    AQinvArepPlusQ, "diagonal")
            with tf.name_scope("off-diagonal_blocks"):
                self.BB = tf.add(tf.matmul(
                    self.LambdaChol[:, :-1],
                    tf.transpose(self.LambdaXChol, [0, 1, 3, 2])),
                    AQinvrep, "off_diagonal")

        with tf.name_scope("posterior_mean"):
            # scale by precision
            LambdaMu = tf.matmul(self.Lambda, tf.expand_dims(self.Mu, -1),
                                 name="Lambda_Mu")

            # compute cholesky decomposition
            self.the_chol = blk.blk_tridiag_chol(self.AA, self.BB)
            # intermediary (mult by R^T)
            ib = blk.blk_chol_inv(self.the_chol[0], self.the_chol[1],
                                  LambdaMu)
            # final result (mult by R)
            self.postX = blk.blk_chol_inv(self.the_chol[0], self.the_chol[1],
                                          ib, lower=False, transpose=True)

        """The determinant of covariance matrix is the square of the
        determinant of Cholesky factor, which is the product of the diagonal
        elements of the block-diagonal.
        """
        with tf.name_scope("log_determinant"):
            def comp_log_det(acc, inputs):
                L = inputs[0]
                return tf.reduce_sum(tf.log(tf.matrix_diag_part(L)), -1)

            self.ln_determinant = -2 * tf.reduce_sum(tf.transpose(tf.scan(
                comp_log_det,
                [tf.transpose(self.the_chol[0], [1, 0, 2, 3])],
                initializer=tf.zeros([self.B]))), -1, name="log_determinant")

    def _sample_n(self, n, seed=None):
        return tf.squeeze(tf.map_fn(self.get_sample, tf.zeros([n, self.B])),
                          -1, "samples")

    def get_sample(self, _=None):
        norm_samp = tf.random_normal([self.B, self.Tt, self.xDim],
                                     name="standard_normal_samples")
        return tf.add(blk.blk_chol_inv(
            self.the_chol[0], self.the_chol[1], tf.expand_dims(norm_samp, -1),
            lower=False, transpose=True), self.postX)

    def _log_prob(self, value):
        return tf.reduce_mean(self.eval_entropy())

    def eval_entropy(self):
        # Compute the entropy of LDS (analogous to prior on smoothness)
        entropy = (self.ln_determinant / 2. +
                   tf.cast(self.xDim * self.Tt, tf.float32) / 2. *
                   (1 + np.log(2 * np.pi)))

        # penalize noise
        if self.p is not None:
            entropy += (self.p *
                        (tf.reduce_sum(tf.log(tf.diag_part(self.Qinv))) +
                            tf.reduce_sum(tf.log(tf.diag_part(self.Q0inv)))))

        return entropy / tf.cast(self.Tt, tf.float32)


class SmoothingPastLDSTimeSeries(SmoothingLDSTimeSeries):
    """SmoothingLDSTimeSeries that uses past observations (lag) in addition to
    current to evaluate the latent.
    """
    def __init__(self, params, Input, xDim, yDim, extra_conds, *args,
                 **kwargs):
        """Initialize SmoothingPastLDSTimeSeries random variable (batch)
        """
        name = kwargs.get("name", "SmoothingPastLDSTimeSeries")
        with tf.name_scope("pad_lag"):
            # manipulate input to include past observations (up to lag)
            if "lag" in params:
                self.lag = params["lag"]
            else:
                self.lag = 1

            Input_ = tf.identity(Input)
            for i in range(self.lag):
                lagged = tf.concat(
                    [tf.reshape(Input_[:, 0, :yDim], [-1, 1, yDim], "t0"),
                     Input_[:, :-1, -yDim:]], 1, "lagged")
                Input_ = tf.concat([Input_, lagged], -1)

        if "name" not in kwargs:
            kwargs["name"] = name
        if "dtype" not in kwargs:
            kwargs["dtype"] = tf.float32
        if "reparameterization_type" not in kwargs:
            kwargs["reparameterization_type"] = FULLY_REPARAMETERIZED
        if "validate_args" not in kwargs:
            kwargs["validate_args"] = True
        if "allow_nan_stats" not in kwargs:
            kwargs["allow_nan_stats"] = False

        super(SmoothingPastLDSTimeSeries, self).__init__(
            # params, Input_, xDim, yDim, extra_conds, *args, **kwargs)
            params, Input_[:, 1:], xDim, yDim, extra_conds[:, 1:],
            *args, **kwargs)
        self._args = (params, Input, xDim, yDim, extra_conds)


class joint_recognition(RandomVariable, Distribution):
    def __init__(self, qg, qz, *args, **kwargs):
        name = kwargs.get("name", "joint_recognition")
        self.qg = qg
        self.qz = qz
        self.agent_dim = self.qg.xDim

        # self.var_list = qg.var_list + qz.var_list
        self.log_vars = qg.log_vars + qz.log_vars

        if "name" not in kwargs:
            kwargs["name"] = name
        if "dtype" not in kwargs:
            kwargs["dtype"] = tf.float32
        if "reparameterization_type" not in kwargs:
            kwargs["reparameterization_type"] = FULLY_REPARAMETERIZED
        if "validate_args" not in kwargs:
            kwargs["validate_args"] = True
        if "allow_nan_stats" not in kwargs:
            kwargs["allow_nan_stats"] = False

        super(joint_recognition, self).__init__(*args, **kwargs)
        self._args = (qg, qz)

    def _log_prob(self, value):

        return tf.add(self.qg.log_prob(value[:, :, :self.agent_dim]),
                      self.qz.log_prob(value[:, :, self.agent_dim:]))

    def _sample_n(self, n, seed=None):

        return tf.concat([self.qg.sample(n), self.qz.sample(n)], -1,
                         name="sample")
