import tensorflow as tf
from tensorflow.contrib.keras import layers, models
import numpy as np
from edward.models import RandomVariable
from tensorflow.contrib.distributions import Categorical, Distribution, Normal
from tensorflow.contrib.distributions import FULLY_REPARAMETERIZED
from tensorflow.contrib.bayesflow import entropy


# def logsumexp(x, axis=None):
#     x_max = tf.reduce_max(x, axis=axis, keep_dims=True)
#     return (tf.log(tf.reduce_sum(tf.exp(x - x_max),
#                                  axis=axis, keep_dims=True)) + x_max)

class GBDS_g_all(RandomVariable, Distribution):
    def __init__(self, GenerativeParams_goalie, GenerativeParams_ball, yDim,
                 y, name='GBDS_g_all', value=None , dtype=tf.float32,
                 reparameterization_type=FULLY_REPARAMETERIZED,
                 validate_args=True, allow_nan_stats=True):

        self.yCols_ball = GenerativeParams_ball['yCols']
        self.yCols_goalie = GenerativeParams_goalie['yCols']
        self.y = y
        self.yDim = yDim

        yDim_ball = len(self.yCols_ball)
        yDim_goalie = len(self.yCols_goalie)

        self.g_goalie = GBDS_g(GenerativeParams_goalie, yDim_goalie, yDim, y,
                               value=tf.gather(value, self.yCols_goalie,
                                               axis=-1))
        self.g_ball = GBDS_g(GenerativeParams_ball, yDim_ball, yDim, y,
                             value=tf.gather(value, self.yCols_ball, axis=-1))

        super(GBDS_g_all, self).__init__(
            name=name, value=value, dtype=dtype,
            reparameterization_type=reparameterization_type,
            validate_args=validate_args, allow_nan_stats=allow_nan_stats)

        self._kwargs['GenerativeParams_goalie'] = GenerativeParams_goalie
        self._kwargs['GenerativeParams_ball'] = GenerativeParams_ball
        self._kwargs['y'] = y
        self._kwargs['yDim'] = yDim

    def _log_prob(self, value):
        log_prob_ball = self.g_ball.log_prob(
            tf.gather(value, self.yCols_ball, axis=-1))
        log_prob_goalie = self.g_goalie.log_prob(
            tf.gather(value, self.yCols_goalie, axis=-1))

        return log_prob_ball + log_prob_goalie

    def getParams(self):
        return self.g_ball.getParams() + self.g_goalie.getParams()

class GBDS_u_all(RandomVariable, Distribution):
    def __init__(self, GenerativeParams_goalie, GenerativeParams_ball, g, y,
                 yDim, PID_params_goalie, PID_params_ball, name='GBDS_u_all',
                 value=None, dtype=tf.float32,
                 reparameterization_type=FULLY_REPARAMETERIZED,
                 validate_args=True, allow_nan_stats=True):

        self.yCols_ball = GenerativeParams_ball['yCols']
        self.yCols_goalie = GenerativeParams_goalie['yCols']
        self.y = y
        self.yDim = yDim
        self.g = g

        yDim_ball = len(self.yCols_ball)
        yDim_goalie = len(self.yCols_goalie)
        g_ball = tf.gather(self.g, self.yCols_ball, axis=-1)
        g_goalie = tf.gather(self.g, self.yCols_goalie, axis=-1)

        self.u_goalie = GBDS_u(GenerativeParams_goalie, g_goalie, y,
                               yDim_goalie, PID_params_goalie,
                               value=tf.gather(value, self.yCols_goalie,
                                               axis=-1))
        self.u_ball = GBDS_u(GenerativeParams_ball, g_ball, y,
                             yDim_ball, PID_params_ball,
                             value=tf.gather(value, self.yCols_ball, axis=-1))

        super(GBDS_u_all, self).__init__(
            name=name, value=value, dtype=dtype,
            reparameterization_type=reparameterization_type,
            validate_args=validate_args, allow_nan_stats=allow_nan_stats)

        self._kwargs['GenerativeParams_goalie'] = GenerativeParams_goalie
        self._kwargs['GenerativeParams_ball'] = GenerativeParams_ball
        self._kwargs['g'] = g
        self._kwargs['y'] = y
        self._kwargs['yDim'] = yDim
        self._kwargs['PID_params_goalie'] = PID_params_goalie
        self._kwargs['PID_params_ball'] = PID_params_ball

    def _log_prob(self, value):
        log_prob_ball = self.u_ball.log_prob(
            tf.gather(value, self.yCols_ball, axis=-1))
        log_prob_goalie = self.u_goalie.log_prob(
            tf.gather(value, self.yCols_goalie, axis=-1))
        return log_prob_ball + log_prob_goalie

    def getParams(self):
        return self.u_ball.getParams() + self.u_goalie.getParams()

class GBDS_u(RandomVariable, Distribution):
    """
    Goal-Based Dynamical System

    Inputs:
    - GenerativeParams: A dictionary of parameters for the model
        Entries include:
        - get_states: function that calculates current state from position
        - pen_eps: Penalty on control signal noise, epsilon
        - pen_sigma: Penalty on goals state noise, sigma
        - pen_g: Two penalties on goal state leaving boundaries (Can be set
                 None)
        - bounds_g: Boundaries corresponding to above penalties
        - NN_postJ_mu: Neural network that parametrizes the mean of the
                       posterior of J (i.e. mu and sigma), conditioned on
                       goals
        - NN_postJ_sigma: Neural network that parametrizes the covariance of
                          the posterior of J (i.e. mu and sigma), conditioned
                          on goals
        - yCols: Columns of Y this agent corresponds to. Used to index columns
                 in real data to compare against generated data.
        - vel: Maximum velocity of each dimension in yCols.
    - yDim: Number of dimensions for this agent
    - yDim_in: Number of total dimensions in the data
    - srng: Theano symbolic random number generator (theano RandomStreams
            object)
    - nrng: Numpy random number generator
    """
    def __init__(self,GenerativeParams, g, y, yDim, PID_params, name='GBDS_u',
                 value=None, dtype=tf.float32,
                 reparameterization_type=FULLY_REPARAMETERIZED,
                 validate_args=True, allow_nan_stats=True):

        self.g = g
        self.y = y
        self.yDim = yDim
        self.B = tf.shape(y)[0]  # batch size
        self.clip = GenerativeParams['clip']

        with tf.name_scope('agent_columns'):
            # which dimensions of Y to predict
            self.yCols = GenerativeParams['yCols']
        with tf.name_scope('velocity'):
            # velocity for each observation dimension (of this agent)
            self.vel = tf.constant(GenerativeParams['vel'], tf.float32)

        with tf.name_scope('PID_controller_params'):
            with tf.name_scope('parameters'):
                # coefficients for PID controller (one for each dimension)
                # https://en.wikipedia.org/wiki/PID_controller#Discrete_implementation
                unc_Kp = PID_params['unc_Kp']
                unc_Ki = PID_params['unc_Ki']
                unc_Kd = PID_params['unc_Kd']
                # create list of PID controller parameters for easy access in
                # getParams
                self.PID_params = [unc_Kp, unc_Ki, unc_Kd]
                # constrain PID controller parameters to be positive
                self.Kp = tf.nn.softplus(unc_Kp, name='Kp')
                self.Ki = tf.nn.softplus(unc_Ki, name='Ki')
                self.Kd = tf.nn.softplus(unc_Kd, name='Kd')
            with tf.name_scope('filter'):
                # calculate coefficients to be placed in convolutional filter
                t_coeff = self.Kp + self.Ki + self.Kd
                t1_coeff = -self.Kp - 2 * self.Kd
                t2_coeff = self.Kd
                # concatenate coefficients into filter
                self.L = tf.concat([t2_coeff, t1_coeff, t_coeff], axis=1,
                                   name='filter')

        with tf.name_scope('control_signal_censoring'):
            # clipping signal
            if self.clip:
                self.clip_range = GenerativeParams['clip_range']
                self.clip_tol = GenerativeParams['clip_tol']
                self.eta = GenerativeParams['eta']
        with tf.name_scope('control_signal_noise'):
            # noise coefficient on control signals
            self.unc_eps = PID_params['unc_eps']
            self.eps = tf.nn.softplus(self.unc_eps, name='eps')
        with tf.name_scope('control_signal_penalty'):
            # penalty on epsilon (noise on control signal)
            if GenerativeParams['pen_eps'] is not None:
                self.pen_eps = GenerativeParams['pen_eps']
            else:
                self.pen_eps = None

        super(GBDS_u, self).__init__(
            name=name, value=value, dtype=dtype,
            reparameterization_type=reparameterization_type,
            validate_args=validate_args, allow_nan_stats=allow_nan_stats)

        self._kwargs['g'] = g
        self._kwargs['y'] = y
        self._kwargs['yDim'] = yDim
        self._kwargs['GenerativeParams'] = GenerativeParams
        self._kwargs['PID_params'] = PID_params

    def get_preds(self, Y, training=False, post_g=None, post_U=None):
        with tf.name_scope('error'):
            # PID Controller for next control point
            if post_g is not None:  # calculate error from posterior goals
                error = post_g[:, 1:] - tf.gather(Y, self.yCols, axis=-1)
            # else:  # calculate error from generated goals
            #     error = next_g - tf.gather(Y, self.yCols, axis=1)
        with tf.name_scope('control_signal_change'):
            Udiff = []
            # get current error signal and corresponding filter
            for i in range(self.yDim):
                signal = error[:, :, i]
                # zero pad beginning
                signal = tf.expand_dims(tf.concat(
                    [tf.zeros([self.B, 2]), signal], 1), -1,
                    name='zero_padding')
                filt = tf.reshape(self.L[i], [-1, 1, 1])
                res = tf.nn.convolution(signal, filt, padding='VALID',
                                        name='signal_conv')
                Udiff.append(res)
            if len(Udiff) > 1:
                Udiff = tf.concat([*Udiff], axis=-1)
            else:
                Udiff = Udiff[0]
        with tf.name_scope('add_noise'):
            if post_g is None:  # Add control signal noise to generated data
                Udiff += self.eps * tf.random_normal(Udiff.shape)
        with tf.name_scope('control_signal'):        
            Upred = post_U[:, :-1] + Udiff
        with tf.name_scope('predicted_position'):
            # get predicted Y
            if self.clip:
                Ypred = (tf.gather(Y, self.yCols, axis=-1) +
                         tf.reshape(self.vel, [1, self.yDim]) *
                         tf.clip_by_value(Upred, -self.clip_range,
                                          self.clip_range,
                                          name='clipped_signal'))
            else:
                Ypred = (tf.gather(Y, self.yCols, axis=-1) +
                         tf.reshape(self.vel, [1, self.yDim]) * Upred)

        return (Upred, Ypred)

    def clip_loss(self, acc, inputs):
        (U_obs, value) = inputs
        left_clip_ind = tf.where(tf.less_equal(
            U_obs, (-self.clip_range + self.clip_tol)),
            name='left_clip_indices')
        right_clip_ind = tf.where(tf.greater_equal(
            U_obs, (self.clip_range - self.clip_tol)),
            name='right_clip_indices')
        non_clip_ind = tf.where(tf.logical_and(
            tf.greater(U_obs, (-self.clip_range + self.clip_tol)),
            tf.less(U_obs, (self.clip_range - self.clip_tol))),
            name='non_clip_indices')
        left_clip_node = Normal(tf.gather_nd(value, left_clip_ind),
                                self.eta, name='left_clip_node')
        right_clip_node = Normal(tf.gather_nd(-value, right_clip_ind),
                                 self.eta, name='right_clip_node')
        non_clip_node = Normal(tf.gather_nd(value, non_clip_ind),
                               self.eta, name='non_clip_node')
        LogDensity = 0.0
        LogDensity += tf.reduce_sum(
            left_clip_node.log_cdf(-1., name='left_clip_logcdf'))
        LogDensity += tf.reduce_sum(
            right_clip_node.log_cdf(-1., name='right_clip_logcdf'))
        LogDensity += tf.reduce_sum(
            non_clip_node.log_prob(tf.gather_nd(U_obs, non_clip_ind),
                name='non_clip_logpdf'))

        return LogDensity

    def _log_prob(self, value):
        '''
        Return a theano function that evaluates the log-density of the
        GenerativeModel.

        g: Goal state time series (sample from the recognition model)
        Y: Time series of positions
        '''
        # Calculate real control signal
        with tf.name_scope('observed_control_signal'):
            U_obs = tf.concat([tf.zeros([self.B, 1, self.yDim]), 
                               (tf.gather(self.y, self.yCols, axis=-1)[:, 1:] -
                                tf.gather(self.y, self.yCols, axis=-1)[:, :-1]) /
                               tf.reshape(self.vel, [1, self.yDim])], 1,
                              name='U_obs')
        # Get predictions for next timestep (at each timestep except for last)
        # disregard last timestep bc we don't know the next value, thus, we
        # can't calculate the error
        with tf.name_scope('next_time_step_pred'):
            Upred, _ = self.get_preds(self.y[:, :-1], training=True,
                                      post_g=self.g, post_U=value)
        
        LogDensity = 0.0
        with tf.name_scope('control_signal_loss'):
            # calculate loss on control signal
            LogDensity += tf.scan(self.clip_loss, (U_obs, value),
                                  initializer=0.0, name='clip_noise')

            # left_clip_ind = tf.where(tf.less_equal(
            #     U_obs, (-self.clip_range + self.clip_tol)),
            #     name='left_clip_indices')
            # right_clip_ind = tf.where(tf.greater_equal(
            #     U_obs, (self.clip_range - self.clip_tol)),
            #     name='right_clip_indices')
            # non_clip_ind = tf.where(tf.logical_and(
            #     tf.greater(U_obs, (-self.clip_range + self.clip_tol)),
            #     tf.less(U_obs, (self.clip_range - self.clip_tol))),
            #     name='non_clip_indices')
            # left_clip_node = Normal(tf.gather_nd(value, left_clip_ind),
            #                         self.eta, name='left_clip_node')
            # right_clip_node = Normal(tf.gather_nd(-value, right_clip_ind),
            #                          self.eta, name='right_clip_node')
            # non_clip_node = Normal(tf.gather_nd(value, non_clip_ind),
            #                        self.eta, name='non_clip_node')
            # LogDensity += tf.reduce_sum(tf.reshape(
            #     left_clip_node.log_cdf(-1., name='left_clip_logcdf'),
            #     [self.B, -1]), axis=1)
            # LogDensity += tf.reduce_sum(tf.reshape(
            #     right_clip_node.log_cdf(-1., name='right_clip_logcdf'),
            #     [self.B, -1]), axis=1)
            # LogDensity += tf.reduce_sum(tf.reshape(
            #     non_clip_node.log_prob(tf.gather_nd(U_obs, non_clip_ind),
            #         name='non_clip_logpdf'), [self.B, -1]), axis=1)

            resU = value[:, 1:] - Upred
            LogDensity -= tf.reduce_sum(resU ** 2 / (2 * self.eps ** 2),
                                        axis=[1, 2])
            LogDensity -= (0.5 * tf.log(2 * np.pi) +
                           tf.reduce_sum(tf.log(self.eps)))
        with tf.name_scope('control_signal_penalty'):
            # penalty on eps
            if self.pen_eps is not None:
                LogDensity -= self.pen_eps * tf.reduce_sum(self.unc_eps)

        return LogDensity

    def getParams(self):
        '''
        Return the learnable parameters of the model
        '''
        rets = self.PID_params #+ [self.unc_eps]
        return rets

class GBDS_g(RandomVariable, Distribution):
    """
    Goal-Based Dynamical System

    Inputs:
    - GenerativeParams: A dictionary of parameters for the model
        Entries include:
        - get_states: function that calculates current state from position
        - pen_eps: Penalty on control signal noise, epsilon
        - pen_sigma: Penalty on goals state noise, sigma
        - pen_g: Two penalties on goal state leaving boundaries (Can be set
                 None)
        - bounds_g: Boundaries corresponding to above penalties
        - NN_postJ_mu: Neural network that parametrizes the mean of the
                       posterior of J (i.e. mu and sigma), conditioned on
                       goals
        - NN_postJ_sigma: Neural network that parametrizes the covariance of
                          the posterior of J (i.e. mu and sigma), conditioned
                          on goals
        - yCols: Columns of Y this agent corresponds to. Used to index columns
                 in real data to compare against generated data.
        - vel: Maximum velocity of each dimension in yCols.
    - yDim: Number of dimensions for this agent
    - yDim_in: Number of total dimensions in the data
    - srng: Theano symbolic random number generator (theano RandomStreams
            object)
    - nrng: Numpy random number generator
    """
    def __init__(self, GenerativeParams, yDim, yDim_in, y, name='GBDS_g',
                 value=None, dtype=tf.float32,
                 reparameterization_type=FULLY_REPARAMETERIZED,
                 validate_args=True, allow_nan_stats=True):

        with tf.name_scope('dimension'):
            self.yDim_in = yDim_in  # dimension of observation input
            self.yDim = yDim
            self.y = y
            self.B = tf.shape(y)[0]  # batch size
            self.C = GenerativeParams['C']  # number of highest-level strategies
            self.K = GenerativeParams['K']  # number of substrategies

        with tf.name_scope('get_states'):
            # function that calculates states from positions
            self.get_states = GenerativeParams['get_states']

        with tf.name_scope('pi'):
            # initial distribution of HMM
            self.log_pi = tf.Variable(initial_value=(np.log(1 / self.K) *
                np.ones((self.C, self.K, 1))), name='log_pi',
                dtype=tf.float32)
            self.pi = tf.nn.softmax(self.log_pi, dim=1, name='pi')

        with tf.name_scope('phi'):
            # distribution of highest-level strategies
            self.log_phi = tf.Variable(initial_value=(np.log(1 / self.C) *
                np.ones((self.C, 1))), name='log_phi', dtype=tf.float32)
            self.phi = tf.nn.softmax(self.log_phi, dim=0, name='phi')

        with tf.name_scope('GMM_NN'):
            # GMM neural networks
            self.GMM_mu_lambda = GenerativeParams['GMM_net_1']
            self.GMM_A = GenerativeParams['GMM_net_2']

        with tf.name_scope('goal_state_penalty'):
            # penalty on sigma (noise on goal state)
            if GenerativeParams['pen_sigma'] is not None:
                self.pen_sigma = GenerativeParams['pen_sigma']
            else:
                self.pen_sigma = None

        with tf.name_scope('boundary_penalty'):
            with tf.name_scope('boundary'):
                # corresponding boundaries for pen_g
                if GenerativeParams['bounds_g'] is not None:
                    self.bounds_g = GenerativeParams['bounds_g']
                else:
                    self.bounds_g = 1.0
            with tf.name_scope('penalty'):
                # penalty on goal state escaping boundaries
                if GenerativeParams['pen_g'] is not None:
                    self.pen_g = GenerativeParams['pen_g']
                else:
                    self.pen_g = None

        with tf.name_scope('transition_matrices_penalty'):
            # penalty on transition matrices of HMM
            if GenerativeParams['pen_A'] is not None:
                self.pen_A = GenerativeParams['pen_A']
            else:
                self.pen_A = None

        with tf.name_scope('velocity'):                    
            # velocity for each observation dimension (of all agents)
            self.all_vel = tf.constant(GenerativeParams['all_vel'],
                                       dtype=tf.float32, name='velocity')

        with tf.name_scope('goal_state_noise'):
            # noise coefficient on goal states
            self.unc_sigma = tf.Variable(
                initial_value=-5 * np.ones((1, self.yDim)), name='unc_sigma',
                dtype=tf.float32)
            self.sigma = tf.nn.softplus(self.unc_sigma, name='sigma')

        super(GBDS_g, self).__init__(
            name=name, value=value, dtype=dtype,
            reparameterization_type=reparameterization_type,
            validate_args=validate_args, allow_nan_stats=allow_nan_stats)

        self._kwargs['y'] = y
        self._kwargs['yDim'] = yDim
        self._kwargs['yDim_in'] = yDim_in
        self._kwargs['GenerativeParams'] = GenerativeParams

    def get_preds(self, Y, training=False, post_g=None,
                  gen_g=None, extra_conds=None):
        """
        Return the predicted next J, g, U, and Y for each point in Y.

        For training: provide post_g, sample from the posterior,
                      which is used to calculate the ELBO
        For generating new data: provide gen_g, the generated goal states up to
                                 the current timepoint
        """
        if training and post_g is None:
            raise Exception(
                "Must provide sample of g from posterior during training")

        with tf.name_scope('states'):
            # get states from position   
            states = self.get_states(Y, max_vel=self.all_vel)
            if extra_conds is not None:
                states = tf.concat([states, extra_conds], axis=-1)

        with tf.name_scope('get_GMM_params'):
            with tf.name_scope('mu'):
                all_mu = tf.reshape(
                    self.GMM_mu_lambda(states)[:, :, :(self.yDim * self.K)],
                    [self.B, -1, self.K, self.yDim], name='reshape_mu')

            with tf.name_scope('lambda'):
                all_lambda = tf.nn.softplus(tf.reshape(
                    self.GMM_mu_lambda(states)[:, :, (self.yDim * self.K):],
                    [self.B, -1, self.K, self.yDim], name='reshape_lambda'),
                    name='softplus_lambda')

            with tf.name_scope('w'):
                with tf.name_scope('A'):
                    all_A = tf.nn.softmax(tf.reshape(
                        self.GMM_A(states)[:, :-1],
                        [self.B, -1, self.C, self.K, self.K],
                        name='reshape_A'), dim=3, name='softmax_A')
                with tf.name_scope('w_k'):
                    pi_repeat = tf.tile(tf.expand_dims(self.pi, 0),
                                        [self.B, 1, 1, 1], name='pi_repeat')
                    w_ck = tf.concat(
                        [tf.expand_dims(pi_repeat, 0),
                        tf.scan(lambda a, x: tf.matmul(x, a),
                            tf.transpose(all_A, [1, 0, 2, 3, 4]),
                            initializer=pi_repeat, name='cum_mult_A')],
                        0, name='w_ck')
                    w_k = tf.transpose(
                        tf.reduce_sum(tf.multiply(w_ck,
                            tf.expand_dims(self.phi, -1)),
                        axis=2, name='integrate_c'), [1, 0, 2, 3], name='w_k')

        with tf.name_scope('next_g'):
                # Draw next goals based on force
            if post_g is not None:  # Calculate next goals from posterior
                next_g = ((tf.reshape(post_g[:, :-1],
                                      [self.B, -1, 1, self.yDim]) +
                           all_mu * all_lambda) / (1 + all_lambda))

            # elif gen_g is not None:  # Generate next goals
            #     # Get external force from GMM
            #     (mu_k, lambda_k), _ = self.sample_GMM(all_mu, all_lambda, all_w)
            #     goal = ((gen_g[(-1,)] + lambda_k[(-1,)] * mu_k[(-1,)]) /
            #             (1 + lambda_k[(-1,)]))
            #     var = self.sigma**2 / (1 + lambda_k[(-1,)])
            #     goal += tf.random.normal(goal.shape) * tf.sqrt(var)
            #     next_g = tf.concat([gen_g[1:], goal], axis=0)
            # else:
            #     raise Exception("Goal states must be provided " +
            #                     "(either posterior or generated)")
        return (all_mu, all_lambda, all_A, w_k, next_g)

    def sample_GMM(self, mu, lmbda, w):
        """
        Sample from GMM based on highest weight component
        """
        # mu = tf.reshape(self.GMM_mu(states), [-1, self.K, self.yDim])
        # lmbda = tf.reshape(self.GMM_lambda(states),
        #                    [-1, self.K, self.yDim])
        # all_w = self.GMM_w(states)

        def select_components(acc, inputs):
            sub_mu, sub_lambda, w = inputs
            z = tf.range(self.K, name='classes')
            p = tf.multinomial(tf.log(tf.squeeze(w, -1)), 1,
                               name='draw')
            component = z[tf.cast(p[0, 0], tf.int32)]

            return sub_mu[:, component, :], sub_lambda[:, component, :]

        (mu_k, lambda_k) = tf.scan(
            select_components, [tf.transpose(mu, [1, 0, 2, 3]),
            tf.transpose(lmbda, [1, 0, 2, 3]),
            tf.transpose(w, [1, 0, 2, 3])],
            initializer=(tf.zeros([self.B, self.yDim]),
                         tf.zeros([self.B, self.yDim])),
            name='select_components')
        updates = {}

        return (mu_k, lambda_k), updates

    def _log_prob(self, value):
        with tf.name_scope('get_params_g_pred'):
            all_mu, all_lambda, all_A, all_w, g_pred = self.get_preds(
                self.y[:, :-1, :], training=True, post_g=value)

        LogDensity = 0.0
        with tf.name_scope('goal_state_loss'):
            w_brdcst = tf.reshape(all_w, [self.B, -1, self.K, 1],
                                  name='reshape_w')
            gmm_res_g = (tf.reshape(value[:, 1:], [self.B, -1, 1, self.yDim],
                                    name='reshape_sample') - g_pred)
            gmm_term = (tf.log(w_brdcst + 1e-8) - ((1 + all_lambda) /
                        (2 * tf.reshape(self.sigma, [1, -1]) ** 2)) *
                        gmm_res_g ** 2)
            gmm_term += (0.5 * tf.log(1 + all_lambda) -
                         0.5 * tf.log(2 * np.pi) -
                         tf.log(tf.reshape(self.sigma, [1, 1, 1, -1])))
            LogDensity += tf.reduce_sum(tf.reduce_logsumexp(tf.reduce_sum(
                gmm_term, axis=-1), axis=-1), axis=-1)

        with tf.name_scope('penalty_A'):
            if self.pen_A is not None:
                # penalize columnwise entropy of transition matrices A's
                LogDensity -= (self.pen_A * tf.recude_sum(
                    entropy.entropy_shannon(
                        probs=tf.transpose(all_A, [0, 1, 2, 4, 3]),
                        name='columnwise_entropy'), axis=[1, 2, 3, 4],
                    name='sum_entropy'))

        with tf.name_scope('goal_and_control_penalty'):
            if self.pen_g is not None:
                # linear penalty on goal state escaping game space
                LogDensity -= (self.pen_g * tf.reduce_sum(
                    tf.cast(tf.greater(value, self.bounds_g), tf.float32),
                    axis=[1, 2]))
                LogDensity -= (self.pen_g * tf.reduce_sum(
                    tf.cast(tf.less(value, -self.bounds_g), tf.float32),
                    axis=[1, 2]))
            if self.pen_sigma is not None:
                # penalty on sigma
                LogDensity -= (self.pen_sigma * tf.reduce_sum(self.unc_sigma))

        return LogDensity

    def getParams(self):
        '''
        Return the learnable parameters of the model
        '''
        rets = (self.GMM_mu_lambda.variables + self.GMM_A.variables +
                [self.log_pi] + [self.log_phi])
        return rets