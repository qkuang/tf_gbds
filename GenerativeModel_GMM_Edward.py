import tensorflow as tf
from tensorflow.contrib.keras import layers, models
import numpy as np
from edward.models import RandomVariable
from tensorflow.contrib.distributions import Distribution, Normal
from tensorflow.contrib.distributions import FULLY_REPARAMETERIZED
from edward.models import MultivariateNormalTriL

def logsumexp(x, axis=None):
    x_max = tf.reduce_max(x, axis=axis, keep_dims=True)
    return (tf.log(tf.reduce_sum(tf.exp(x - x_max),
                                 axis=axis, keep_dims=True)) + x_max)

class GBDS_g_all(RandomVariable, Distribution):
    def __init__(self, GenerativeParams_goalie, GenerativeParams_ball, yDim,
                 y, name="GBDS_g_all", value=None , dtype=tf.float32,
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
    def __init__(self,GenerativeParams_goalie, GenerativeParams_ball, g, y,
                 yDim, PID_params_goalie, PID_params_ball, name="GBDS_u_all",
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
        g_ball = tf.gather(self.g, self.yCols_ball, axis=1)
        g_goalie = tf.gather(self.g, self.yCols_goalie, axis=1)

        self.u_goalie = GBDS_u(GenerativeParams_goalie, g_goalie, y,
                               yDim_goalie, PID_params_goalie,
                               value=tf.gather(value, self.yCols_goalie,
                                               axis=1))
        self.u_ball = GBDS_u(GenerativeParams_ball, g_ball, y, yDim_ball,
                             PID_params_ball,
                             value=tf.gather(value, self.yCols_ball, axis=1))

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
            tf.gather(value, self.yCols_ball, axis=1))
        log_prob_goalie = self.u_goalie.log_prob(
            tf.gather(value, self.yCols_goalie, axis=1))
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
    def __init__(self,GenerativeParams, g, y, yDim, PID_params, name="GBDS_u",
                 value=None, dtype=tf.float32,
                 reparameterization_type=FULLY_REPARAMETERIZED,
                 validate_args=True, allow_nan_stats=True):

        self.g = g
        self.y = y
        self.yDim = yDim

        with tf.name_scope('control_signal_penalty'):
            # penalty on epsilon (noise on control signal)
            if 'pen_eps' in GenerativeParams:
                self.pen_eps = GenerativeParams['pen_eps']
            else:
                self.pen_eps = None
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
                self.Kp = tf.nn.softplus(unc_Kp)
                self.Ki = tf.nn.softplus(unc_Ki)
                self.Kd = tf.nn.softplus(unc_Kd)
            with tf.name_scope('filter'):
                # calculate coefficients to be placed in convolutional filter
                t_coeff = self.Kp + self.Ki + self.Kd
                t1_coeff = -self.Kp - 2 * self.Kd
                t2_coeff = self.Kd
                # concatenate coefficients into filter
                self.L = tf.concat([t2_coeff, t1_coeff, t_coeff], axis=1,
                                   name='filter')

        with tf.name_scope('control_signal_noise'):
            # noise coefficient on control signals
            self.unc_eps = PID_params['unc_eps']
            self.eps = tf.nn.softplus(self.unc_eps)

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
                error = post_g[1:] - tf.gather(Y, self.yCols, axis=1)
            # else:  # calculate error from generated goals
            #     error = next_g - tf.gather(Y, self.yCols, axis=1)
        with tf.name_scope('control_signal_change'):
            Udiff = []
            for i in range(self.yDim):
                # get current error signal and corresponding filter
                signal = error[:, i]
                filt = tf.reshape(self.L[i], [-1, 1, 1, 1])
                # zero pad beginning
                signal = tf.reshape(tf.concat([tf.zeros(2), signal], axis=0),
                                    [1, -1, 1, 1])
                res = tf.nn.convolution(signal, filt, padding="VALID")
                res = tf.reshape(res, [-1, 1])
                Udiff.append(res)
            if len(Udiff) > 1:
                Udiff = tf.concat([*Udiff], axis=1)
            else:
                Udiff = Udiff[0]
        with tf.name_scope('add_noise'):
            if post_g is None:  # Add control signal noise to generated data
                Udiff += self.eps * tf.random_normal(Udiff.shape)
        with tf.name_scope('control_signal'):        
            Upred = post_U[:-1] + Udiff
        with tf.name_scope('predicted_position'):
            # get predicted Y
            Ypred = (tf.gather(Y, self.yCols, axis=1) +
                     tf.reshape(self.vel, [1, self.yDim]) *
                     tf.clip_by_value(Upred, -1, 1, name='clipped_signal'))

        return (Upred, Ypred)       

    def _log_prob(self, value):
        '''
        Return a theano function that evaluates the log-density of the
        GenerativeModel.

        g: Goal state time series (sample from the recognition model)
        Y: Time series of positions
        '''
        # Calculate real control signal
        with tf.name_scope('observed_control_signal'):
            U_obs = tf.concat([tf.zeros([1, self.yDim]), 
                               (tf.gather(self.y, self.yCols, axis=1)[1:] -
                                tf.gather(self.y, self.yCols, axis=1)[:-1]) /
                               tf.reshape(self.vel, [1, self.yDim])], 0,
                              name='U_obs')
        # Get predictions for next timestep (at each timestep except for last)
        # disregard last timestep bc we don't know the next value, thus, we
        # can't calculate the error
        with tf.name_scope('next_time_step_pred'):
            Upred, Ypred = self.get_preds(self.y[:-1], training=True,
                                          post_g=self.g, post_U=value)
        with tf.name_scope('control_signal_loss'):
            # calculate loss on control signal
            tol = 1e-5
            eta = 1e-6
            for i in range(self.yDim):
                left_clip_ind = tf.where(
                    tf.less_equal(U_obs[:, i], -1.+tol),
                    name='left_clip_indices')
                right_clip_ind = tf.where(
                    tf.greater_equal(U_obs[:, i], 1.-tol),
                    name='right_clip_indices')
                non_clip_ind = tf.where(
                    tf.logical_and(tf.greater(U_obs[:, i], -1.+tol),
                    tf.less(U_obs[:, i], 1.-tol)), name='non_clip_indices')
                left_clip_node = Normal(
                    tf.gather_nd(value[:, i], left_clip_ind), eta,
                    name='left_clip_node')
                right_clip_node = Normal(
                    -tf.gather_nd(value[:, i], right_clip_ind), eta,
                    name='right_clip_node')
                non_clip_node = Normal(
                    tf.gather_nd(value[:, i], non_clip_ind), eta,
                    name='non_clip_node')
                LogDensity = tf.reduce_sum(
                    left_clip_node.log_cdf(-1., name='left_clip_logcdf'))
                LogDensity += tf.reduce_sum(
                    right_clip_node.log_cdf(-1., name='right_clip_logcdf'))
                LogDensity += tf.reduce_sum(non_clip_node.log_prob(
                    tf.gather_nd(U_obs[:, i], non_clip_ind),
                    name='non_clip_logpdf'))

            resU = value[1:] - Upred
            LogDensity -= tf.reduce_sum(resU**2 / (2 * self.eps**2))
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
    def __init__(self, GenerativeParams, yDim, yDim_in, y, name="GBDS_g",
                 value=None, dtype=tf.float32,
                 reparameterization_type=FULLY_REPARAMETERIZED,
                 validate_args=True, allow_nan_stats=True):

        with tf.name_scope('dimension'):
            self.yDim_in = yDim_in  # dimension of observation input
            self.yDim = yDim
            self.y = y
            self.B = tf.shape(y)[0]  # batch size
        with tf.name_scope('get_states'):
            # function that calculates states from positions
            self.get_states = GenerativeParams['get_states']

        with tf.name_scope('pi'):
            # initial distribution of HMM
            self.log_pi = tf.Variable(initial_value=(np.log(1 / self.K) *
                np.ones((self.C, self.K, 1))), name='log_pi',
                dtype=tf.float32)
            self.pi = tf.nn.softmax(log_pi, dim=1, name='pi')

        with tf.name_scope('phi'):
            # distribution of highest-level strategies
            self.log_phi = tf.Variable(initial_value=(np.log(1 / self.C) *
                np.ones((self.C, 1))), name='log_phi', dtype=tf.float32)
            self.phi = tf.nn.softmax(log_phi, dim=0, name='phi')

        with tf.name_scope('GMM_NN'):
            # GMM neural networks
            self.C = GenerativeParams['C']  # number of highest-level strategies
            self.K = GenerativeParams['K']  # number of substrategies
            self.NN_1 = GenerativeParams['GMM_net_1']
            self.NN_2 = GenerativeParams['GMM_net_2']

            with tf.name_scope('mu'):
                output_mu = tf.reshape(
                    self.NN_1.output[:, :, :(yDim * self.K)],
                    [self.B, -1, self.K, yDim], name='reshape_mu')

            with tf.name_scope('lambda'):
                output_lambda = tf.nn.softplus(tf.reshape(
                    self.NN_1.output[:, :, (yDim * self.K):],
                    [self.B, -1, self.K, yDim],
                    name='reshape_lambda'), name='softplus_lambda')

            self.GMM_mu_lambda = models.Model(
                inputs=self.NN_1.input, outputs=[output_mu, output_lambda],
                name='GMM_mu_lambda')

            # output_w = tf.nn.softmax(
            #     self.GMM_net.output[:, (2 * yDim * self.GMM_k):])
            # self.GMM_w = models.Model(self.GMM_net.input, output_w,
            #                           name='GMM_w')

            with tf.name_scope('A'):
                output_A = tf.nn.softmax(tf.reshape(
                    self.NN_2.output[:, :-1],
                    [self.B, -1, self.C, self.K, self.K],
                    name='reshape_A'), dim=-2, name='softmax_A')

            with tf.name_scope('w_k'):
                pi_repeat = tf.tile(tf.expand_dims(self.pi, 0),
                                    [self.B, 1, 1, 1],
                                    name='pi_repeat')
                w_ck = tf.concat(
                    [tf.expand_dims(pi_repeat, 0),
                    tf.scan(lambda a, x: tf.matmul(x, a),
                        tf.transpose(output_A, [1, 0, 2, 3, 4]),
                        initializer=pi_repeat, name='cum_mult_A')],
                    0, name='w_ck')
                w_k = tf.transpose(
                    tf.reduce_sum(tf.multiply(w_ck,
                        tf.expand_dims(self.phi, -1)),
                    axis=2, name='integrate_c'), [1, 0, 2, 3], name='w_k')

            self.GMM_w = models.Model(inputs=self.NN_2.input, outputs=w_k,
                                      name='GMM_w')

        # with tf.name_scope('goal_state_penalty'):
        #     # penalty on sigma (noise on goal state)
        #     if 'pen_sigma' in GenerativeParams:
        #         self.pen_sigma = GenerativeParams['pen_sigma']
        #     else:
        #         self.pen_sigma = None

        # with tf.name_scope('boundary_penalty'):
        #     with tf.name_scope('penalty'):
        #         # penalties on goal state passing boundaries
        #         if 'pen_g' in GenerativeParams:
        #             self.pen_g = GenerativeParams['pen_g']
        #         else:
        #             self.pen_g = (None, None)

        #     with tf.name_scope('boundary'):
        #         # corresponding boundaries for pen_g
        #         if 'bounds_g' in GenerativeParams:
        #             self.bounds_g = GenerativeParams['bounds_g']
        #         else:
        #             self.bounds_g = (1.0, 1.5)

        with tf.name_scope('velocity'):                    
            # velocity for each observation dimension (of all agents)
            self.all_vel = tf.constant(GenerativeParams['all_vel'],
                                       tf.float32, name='velocity')

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
            # (all_mu, all_lambda, all_w, mu_k,
            #     lambda_k), updates = self.sample_GMM(states)
            all_mu, all_lambda = self.GMM_mu_lambda(states)
            all_w = self.GMM_w(states)

        with tf.name_scope('next_g'):
                # Draw next goals based on force
            if post_g is not None:  # Calculate next goals from posterior
                next_g = ((tf.reshape(post_g[:, :-1],
                                      [self.B, -1, 1, yDim]) +
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
        return (all_mu, all_lambda, all_w, next_g)

    def _log_prob(self, value):
        with tf.name_scope('get_params_g_pred'):
            all_mu, all_lmbda, all_w, g_pred = self.get_preds(
                self.y[:, :-1, :], training=True, post_g=value)

        with tf.name_scope('goal_state_loss'):                              
            w_brdcst = tf.reshape(all_w, [self.B, -1, self.K, 1])
            gmm_res_g = (tf.reshape(value[:, 1:], [self.B, -1, 1, yDim]) -
                         g_pred)
            gmm_term = (tf.log(w_brdcst + 1e-8) - ((1 + all_lambda) /
                (2 * tf.reshape(self.sigma, [1, 1, 1, -1]) ** 2)) *
                gmm_res_g ** 2)
            gmm_term += (0.5 * tf.log(1 + all_lambda) -
                         0.5 * tf.log(2 * np.pi) -
                         tf.log(tf.reshape(self.sigma, [1, 1, 1, -1])))
            LogDensity = tf.reduce_sum(logsumexp(tf.reduce_sum(
                gmm_term, axis=-1), axis=-1))

        # with tf.name_scope('penalty_A'):
            # column entropy

        with tf.name_scope('goal_and_control_penalty'):
            # linear penalty on goal state escaping game space
            # rescaling penalty to be implemented
            if self.pen_g[0] is not None:
                LogDensity -= (self.pen_g[0] * tf.reduce_sum(
                    tf.nn.relu(g_pred - self.bounds_g[0])) / self.B)
                LogDensity -= (self.pen_g[0] * tf.reduce_sum(
                    tf.nn.relu(-g_pred - self.bounds_g[0])) / self.B)
            if self.pen_g[1] is not None:
                LogDensity -= (self.pen_g[1] * tf.reduce_sum(
                    tf.nn.relu(g_pred - self.bounds_g[1])) / self.B)
                LogDensity -= (self.pen_g[1] * tf.reduce_sum(
                    tf.nn.relu(-g_pred - self.bounds_g[1])) / self.B)
            if self.pen_sigma is not None:
                # penalty on sigma
                LogDensity -= (self.pen_sigma *
                    tf.reduce_sum(self.unc_sigma) / self.B)

        return LogDensity

    def getParams(self):
        '''
        Return the learnable parameters of the model
        '''
        rets = self.GMM_net.variables + [self.log_pi] + [self.log_phi]
        return rets