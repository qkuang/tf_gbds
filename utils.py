import tensorflow as tf
import math
from os.path import expanduser
import h5py
from scipy.stats import norm
import numpy as np
import pandas as pd
from tensorflow.contrib.keras import layers
from tensorflow.contrib.keras import constraints, models
from matplotlib.colors import Normalize
import edward as ed
from edward.models import Exponential, Gamma, PointMass
import six
from tf_gbds.layers import PKBiasLayer, PKRowBiasLayer


class set_cbar_zero(Normalize):
    """set_cbar_zero(midpoint = float)       default: midpoint = 0.
    Normalizes and sets the center of any colormap to the desired value which
    is set using midpoint.
    """
    def __init__(self, vmin=None, vmax=None, midpoint=0., clip=False):
        self.midpoint = midpoint
        Normalize.__init__(self, vmin, vmax, clip)

    def __call__(self, value, clip=None):
        x, y = ([min(self.vmin, -self.vmax), self.midpoint, max(self.vmax,
                                                                -self.vmin)],
                [0, 0.5, 1])
        return np.ma.masked_array(np.interp(value, x, y))


def gauss_convolve(x, sigma, pad_method='edge_pad'):
    """Smoothing with gaussian convolution
    Pad Methods:
        * edge_pad: pad with the values on the edges
        * extrapolate: extrapolate the end pad based on dx at the end
        * zero_pad: pad with zeros
    """
    method_types = ['edge_pad', 'extrapolate', 'zero_pad']
    if pad_method not in method_types:
        raise Exception("Pad method not recognized")
    edge = int(math.ceil(5 * sigma))
    fltr = norm.pdf(range(-edge, edge), loc=0, scale=sigma)
    fltr = fltr / sum(fltr)

    szx = x.size

    if pad_method == 'edge_pad':
        buff = np.ones(edge)
        xx = np.append((buff * x[0]), x)
        xx = np.append(xx, (buff * x[-1]))
    elif pad_method == 'extrapolate':
        buff = np.ones(edge)
        # linear extrapolation for end edge buffer
        end_dx = x[-1] - x[-2]
        end_buff = np.cumsum(end_dx * np.ones(edge)) + x[-1]
        xx = np.append((buff * x[0]), x)
        xx = np.append(xx, end_buff)
    else:
        # zero pad
        buff = np.zeros(edge)
        xx = np.append(buff, x)
        xx = np.append(xx, buff)

    y = np.convolve(xx, fltr, mode='valid')
    y = y[:szx]
    return y


def smooth_trial(trial, sigma=4.0, pad_method='extrapolate'):
    """Apply Gaussian convolution Smoothing method to real data
    """
    rtrial = trial.copy()
    for i in range(rtrial.shape[1]):
        rtrial[:, i] = gauss_convolve(rtrial[:, i], sigma,
                                      pad_method=pad_method)
    return rtrial


# def gen_data(n_trials, n_obs, sigma=np.log1p(np.exp(-5. * np.ones((1, 2)))),
#              eps=np.log1p(np.exp(-10.)), Kp=1, Ki=0, Kd=0,
#              vel=1e-2 * np.ones((3))):
#     """Generate fake data to test the accuracy of the model
#     """
#     p = []
#     g = []

#     for _ in range(n_trials):
#         p_b = np.zeros((n_obs, 2), np.float32)
#         p_g = np.zeros((n_obs, 1), np.float32)
#         g_b = np.zeros((n_obs, 2), np.float32)
#         prev_error_b = 0
#         prev_error_g = 0
#         int_error_b = 0
#         int_error_g = 0

#         init_b_x = np.pi * (np.random.rand() * 2 - 1)
#         g_b_x_mu = 0.25 * np.sin(2. * (np.linspace(0, 2 * np.pi, n_obs) -
#                                        init_b_x))
#         init_b_y = np.pi * (np.random.rand() * 2 - 1)
#         g_b_y_mu = 0.25 * np.sin(2. * (np.linspace(0, 2 * np.pi, n_obs) -
#                                        init_b_y))
#         g_b_mu = np.hstack([g_b_x_mu.reshape(n_obs, 1),
#                             g_b_y_mu.reshape(n_obs, 1)])
#         g_b_lambda = np.array([16, 16], np.float32)
#         g_b[0] = g_b_mu[0]

#         for t in range(n_obs - 1):
#             g_b[t + 1] = ((g_b[t] + g_b_lambda * g_b_mu[t + 1]) /
#                           (1 + g_b_lambda))
#             var = sigma ** 2 / (1 + g_b_lambda)
#             g_b[t + 1] += (np.random.randn(1, 2) * np.sqrt(var)).reshape(2,)

#             error_b = g_b[t + 1] - p_b[t]
#             int_error_b += error_b
#             der_error_b = error_b - prev_error_b
#             u_b = (Kp * error_b + Ki * int_error_b + Kd * der_error_b +
#                    eps * np.random.randn(2,))
#             prev_error_b = error_b
#             p_b[t + 1] = p_b[t] + vel[1:] * np.clip(u_b, -1, 1)

#             error_g = p_b[t + 1, 1] - p_g[t]
#             int_error_g += error_g
#             der_error_g = error_g - prev_error_g
#             u_g = (Kp * error_g + Ki * int_error_g + Kd * der_error_g +
#                    eps * np.random.randn())
#             prev_error_g = error_g
#             p_g[t + 1] = p_g[t] + vel[0] * np.clip(u_g, -1, 1)

#         p.append(np.hstack((p_g, p_b)))
#         g.append(g_b)

#     return p, g


def gen_data(n_trials, n_obs, sigma=np.log1p(np.exp(-5. * np.ones((1, 3)))),
             eps=np.log1p(np.exp(-10.)), Kp=1, Ki=0, Kd=0,
             vel=1. * np.ones((3))):

    p = []
    g = []

    for _ in range(n_trials):
        p_b = np.zeros((n_obs, 2), np.float32)
        p_g = np.zeros((n_obs, 1), np.float32)
        g_b = np.zeros((n_obs, 2), np.float32)
        g_g = np.zeros((n_obs, 1), np.float32)
        prev_error_b = 0
        prev_error_g = 0
        prev2_error_b = 0
        prev2_error_g = 0
        u_b = 0
        u_g = 0

        init_b_x = np.pi * (np.random.rand() * 2 - 1)
        g_b_x_mu = (np.linspace(0, 0.975, n_obs) + 0.02 * np.sin(2. *
                    (np.linspace(0, 2 * np.pi, n_obs) - init_b_x)))

        init_b_y = np.pi * (np.random.rand() * 2 - 1)
        g_b_y_mu = (np.linspace(-0.2 + (np.random.rand() * 0.1 - 0.05),
                                0.4 + (np.random.rand() * 0.1 - 0.05),
                                n_obs) +
                    0.05 * np.sin(2. * (np.linspace(0, 2 * np.pi, n_obs) -
                                        init_b_y)))
        g_b_mu = np.hstack([g_b_x_mu.reshape(n_obs, 1),
                            g_b_y_mu.reshape(n_obs, 1)])
        g_b_lambda = np.array([1e4, 1e4])
        g_b[0] = g_b_mu[0]

        init_g = np.pi * (np.random.rand() * 2 - 1)
        g_g_mu = (np.linspace(-0.2 + (np.random.rand() * 0.1 - 0.05),
                              0.4 + (np.random.rand() * 0.1 - 0.05), n_obs) +
                  0.05 * np.sin(2. * (np.linspace(0, 2 * np.pi, n_obs) -
                                      init_g)))
        g_g_lambda = 1e4
        g_g[0] = g_g_mu[0]

        for t in range(n_obs - 1):
            g_b[t + 1] = ((g_b[t] + g_b_lambda * g_b_mu[t + 1]) /
                          (1 + g_b_lambda))
            var_b = sigma[0, 1:] ** 2 / (1 + g_b_lambda)
            g_b[t + 1] += (np.random.randn(1, 2) * np.sqrt(var_b)).reshape(2,)

            error_b = g_b[t + 1] - p_b[t]
            u_b += ((Kp + Ki + Kd) * error_b - (Kp + 2 * Kd) * prev_error_b +
                    Kd * prev2_error_b + eps * np.random.randn(2,))
            p_b[t + 1] = np.clip(p_b[t] + vel[1:] * np.clip(u_b, -1, 1),
                                 -1, 1)
            prev2_error_b = prev_error_b
            prev_error_b = error_b

            g_g[t + 1] = ((g_g[t] + g_g_lambda * g_g_mu[t + 1]) /
                          (1 + g_g_lambda))
            var_g = sigma[0, 0] ** 2 / (1 + g_g_lambda)
            g_g[t + 1] += np.random.randn() * np.sqrt(var_g)

            error_g = g_g[t + 1] - p_g[t]
            u_g += ((Kp + Ki + Kd) * error_g - (Kp + 2 * Kd) * prev_error_g +
                    Kd * prev2_error_g + eps * np.random.randn())
            p_g[t + 1] = np.clip(p_g[t] + vel[0] * np.clip(u_g, -1, 1), -1, 1)
            prev2_error_g = prev_error_g
            prev_error_g = error_g

        p.append(np.hstack((p_g, p_b)))
        g.append(np.hstack((g_g, g_b)))

    return p, g


def load_data(hps):
    """ Generate synthetic data set or load real data from local directory
    """
    train_data = []
    val_data = []
    if hps.syn_data:
        data, goals = gen_data(
            n_trials=2000, n_obs=100, Kp=0.5, Ki=0.2, Kd=0.1)
        np.random.seed(hps.seed)  # set seed for consistent train/val split
        val_goals = []
        for (trial_data, trial_goals) in zip(data, goals):
            if np.random.rand() <= hps.train_ratio:
                train_data.append(trial_data)
            else:
                val_data.append(trial_data)
                val_goals.append(trial_goals)
        np.save(hps.model_dir + "/train_data", train_data)
        np.save(hps.model_dir + "/val_data", val_data)
        np.save(hps.model_dir + "/val_goals", val_goals)

        train_conds = None
        val_conds = None
        train_ctrls = None
        val_ctrls = None

    elif hps.data_dir is not None:
        datafile = h5py.File(hps.data_dir, 'r')
        if "trajectories" in datafile:
            trajs = np.array(datafile["trajectories"], np.float32)
        else:
            raise Exception("Trajectories must be provided.")
        if "conditions" in datafile:
            conds = np.array(datafile["conditions"], np.float32)
        else:
            conds = None
            train_conds = None
            val_conds = None
        if "control" in datafile:
            ctrls = np.array(datafile["control"], np.float32)
        else:
            ctrls = None
            train_ctrls = None
            val_ctrls = None
        datafile.close()

        if hps.val:
            if hps.train_ratio is None:
                train_ratio = 0.85
            else:
                train_ratio = hps.train_ratio

            train_ind = []
            val_ind = []
            np.random.seed(hps.seed)  # set seed for consistent train/val split
            for i in range(len(trajs)):
                if np.random.rand() <= hps.train_ratio:
                    train_ind.append(i)
                else:
                    val_ind.append(i)
            np.save(hps.model_dir + '/train_indices', train_ind)
            np.save(hps.model_dir + '/val_indices', val_ind)

            train_data = trajs[train_ind]
            val_data = trajs[val_ind]
            if conds is not None:
                train_conds = conds[train_ind]
                val_conds = conds[val_ind]
            if ctrls is not None:
                train_ctrls = ctrls[train_ind]
                val_ctrls = ctrls[val_ind]
        else:
            train_data = trajs
            val_data = None
            if conds is not None:
                train_conds = conds
                val_conds = None
            if ctrls is not None:
                train_ctrls = ctrls
                val_ctrls = None

    else:
        raise Exception("Data must be provided (either real or synthetic).")

    return train_data, train_conds, train_ctrls, val_data, val_conds, val_ctrls


def get_max_velocities(datasets, dim):
    """Get the maximium velocities from data
    """
    max_vel = [[] for _ in range(dim)]
    for d in range(len(datasets)):
        for i in range(len(datasets[d])):
            for c in range(dim):
                if np.abs(np.diff(datasets[d][i][:, c])).max() > 0.001:
                     max_vel[c].append(
                        np.abs(np.diff(datasets[d][i][:, c])).max())

    return np.round(np.array([max(vel) for vel in max_vel]), decimals=3)


def get_vel(traj, max_vel):
    """Input a time series of positions and include velocities for each
    coordinate in each row
    """
    with tf.name_scope('get_velocity'):
        vel = tf.pad(tf.divide(traj[:, 1:] - traj[:, :-1], max_vel,
                               name='standardize'),
                     [[0, 0], [1, 0], [0, 0]], name='pad_zero')
        states = tf.concat([traj, vel], -1, name='states')

        return states


def get_accel(traj, max_vel):
    """Input a time series of positions and include velocities and acceleration
    for each coordinate in each row
    """
    with tf.name_scope('get_acceleration'):
        states = get_vel(traj, max_vel)
        accel = traj[:, 2:] - 2 * traj[1:-1] + traj[:-2]
        accel = tf.pad(accel, [[0, 0], [2, 0], [0, 0]])
        states = tf.concat([states, accel], -1, name='states')

        return states


def get_agent_params(name, agent_dim, agent_cols,
                     obs_dim, state_dim, extra_dim,
                     n_layers_gen, hidden_dim_gen, GMM_K, sigma,
                     boundaries_goal, penalty_goal, PKLparams, vel,
                     latent_ctrl, lag, n_layers_rec, hidden_dim_rec,
                     penalty_Q, penalty_ctrl, ctrl_residual_tolerance,
                     signal_clip, clip_range, clip_tol, eta,
                     penalty_ctrl_error):

    with tf.variable_scope('%s_params' % name):
        GMM_NN, _ = get_network('goal_GMM', (state_dim + extra_dim),
                                (GMM_K * agent_dim * 2 + GMM_K),
                                hidden_dim_gen, n_layers_gen, PKLparams)
        g0 = init_g0_params(agent_dim, GMM_K)

        g_q_params = get_rec_params(obs_dim, extra_dim, agent_dim, lag,
                                    n_layers_rec, hidden_dim_rec,
                                    penalty_Q, PKLparams, 'goal_posterior')

        if latent_ctrl:
            u_q_params = get_rec_params(obs_dim, extra_dim, agent_dim, lag,
                                        n_layers_rec, hidden_dim_rec,
                                        penalty_Q, PKLparams,
                                        'control_posterior')
        else:
            u_q_params = None

        agent_vel = vel[agent_cols]
        PID_p = get_PID_priors(agent_dim, agent_vel)
        PID_q = get_PID_posteriors(agent_dim)

        params = dict(
            name=name, agent_dim=agent_dim, agent_cols=agent_cols,
            obs_dim=obs_dim, state_dim=state_dim, extra_dim=extra_dim,
            GMM_K=GMM_K, GMM_NN=GMM_NN, g0=g0, sigma=sigma,
            bounds_g=boundaries_goal, pen_g=penalty_goal,
            g_q_params=g_q_params, agent_vel=agent_vel, latent_u=latent_ctrl,
            u_q_params=u_q_params, PID_priors=PID_p, PID_posteriors=PID_q,
            pen_u=penalty_ctrl, u_res_tol=ctrl_residual_tolerance,
            clip=signal_clip, clip_range=clip_range, clip_tol=clip_tol,
            eta=eta, pen_ctrl_error=penalty_ctrl_error)

        return params


def get_network(name, input_dim, output_dim, hidden_dim, num_layers,
                PKLparams=None, batchnorm=False, is_shooter=False,
                row_sparse=False, add_pklayers=False, filt_size=None):
    """Returns a NN with the specified parameters.
    Also returns a list of PKBias layers
    """

    with tf.variable_scope(name):
        M = models.Sequential(name='NN')
        PKbias_layers = []
        M.add(layers.InputLayer(input_shape=(None, input_dim), name='Input'))
        if batchnorm:
            M.add(layers.BatchNormalization(name='BatchNorm'))
        if filt_size is not None:
            M.add(layers.ZeroPadding1D(padding=(filt_size - 1, 0),
                                       name='ZeroPadding'))
            M.add(layers.Conv1D(filters=hidden_dim, kernel_size=filt_size,
                                padding='valid', activation=tf.nn.relu,
                                name='Conv1D'))

        for i in range(num_layers):
            with tf.variable_scope('PK_Bias'):
                if is_shooter and add_pklayers:
                    if row_sparse:
                        PK_bias = PKRowBiasLayer(
                            M, PKLparams,
                            name='PKRowBias_%s' % (i + 1))
                    else:
                        PK_bias = PKBiasLayer(
                            M, PKLparams,
                            name='PKBias_%s' % (i + 1))
                    PKbias_layers.append(PK_bias)
                    M.add(PK_bias)

            if i == num_layers - 1:
                M.add(layers.Dense(
                    output_dim, activation='linear',
                    kernel_initializer=tf.random_normal_initializer(
                        stddev=0.1), name='Dense_%s' % (i + 1)))
            else:
                M.add(layers.Dense(
                    hidden_dim, activation='relu',
                    kernel_initializer=tf.orthogonal_initializer(),
                    name='Dense_%s' % (i + 1)))

        return M, PKbias_layers


def get_rec_params(obs_dim, extra_dim, agent_dim, lag, n_layers, hidden_dim,
                   penalty_Q=None, PKLparams=None, name='recognition'):
    """Return a dictionary of timeseries-specific parameters for recognition
       model
    """

    with tf.variable_scope('%s_params' % name):
        Mu_net, PKbias_layers_mu = get_network(
            'Mu_NN', (obs_dim * (lag + 1) + extra_dim), agent_dim, hidden_dim,
            n_layers, PKLparams)
        Lambda_net, PKbias_layers_lambda = get_network(
            'Lambda_NN', obs_dim * (lag + 1) + extra_dim, agent_dim ** 2,
            hidden_dim, n_layers, PKLparams)
        LambdaX_net, PKbias_layers_lambdaX = get_network(
            'LambdaX_NN', obs_dim * (lag + 1) + extra_dim, agent_dim ** 2,
            hidden_dim, n_layers, PKLparams)

        Dyn_params = dict(
            A=tf.Variable(
                .9 * np.eye(obs_dim), name='A', dtype=tf.float32),
            QinvChol=tf.Variable(
                np.eye(obs_dim), name='QinvChol', dtype=tf.float32),
            Q0invChol=tf.Variable(
                np.eye(obs_dim), name='Q0invChol', dtype=tf.float32))

        rec_params = dict(
            Dyn_params=Dyn_params,
            NN_Mu=dict(network=Mu_net,
                       PKbias_layers=PKbias_layers_mu),
            NN_Lambda=dict(network=Lambda_net,
                           PKbias_layers=PKbias_layers_lambda),
            NN_LambdaX=dict(network=LambdaX_net,
                            PKbias_layers=PKbias_layers_lambdaX),
            lag=lag)

        with tf.name_scope('penalty_Q'):
            if penalty_Q is not None:
                rec_params['p'] = penalty_Q

        return rec_params


# def get_gen_params_GBDS_GMM(obs_dim_agent, obs_dim, extra_dim, add_accel,
#                             yCols_agent, nlayers_gen, hidden_dim_gen,
#                             K, PKLparams, vel, sigma, eps,
#                             penalty_ctrl_error, boundaries_g, penalty_g,
#                             latent_u, clip, clip_range, clip_tol, eta, name):
#     """Return a dictionary of timeseries-specific parameters for generative
#        model
#     """

#     with tf.name_scope('get_states_%s' % name):
#         if add_accel:
#             get_states = get_accel
#             state_dim = obs_dim * 3
#         else:
#             get_states = get_vel
#             state_dim = obs_dim * 2

#     with tf.name_scope('gen_GMM_%s' % name):
#         GMM_net, _ = get_network('gen_GMM_%s' % name, (state_dim + extra_dim),
#                                  (K * obs_dim_agent * 2 + K),
#                                  hidden_dim_gen, nlayers_gen, PKLparams)

#     with tf.name_scope('PID_%s' % name):
#         PID_params = init_PID_params(name, obs_dim_agent, vel[yCols_agent])

#     with tf.name_scope('initial_goal_%s' % name):
#         g0_params = init_g0_params(name, obs_dim_agent, K)

#     gen_params = dict(all_vel=vel,
#                       vel=vel[yCols_agent],
#                       yCols=yCols_agent,  # which columns belong to the agent
#                       sigma=sigma,
#                       eps=eps,
#                       pen_ctrl_error=penalty_ctrl_error,
#                       bounds_g=boundaries_g,
#                       pen_g=penalty_g,
#                       latent_u=latent_u,
#                       clip=clip,
#                       clip_range=clip_range,
#                       clip_tol=clip_tol,
#                       eta=eta,
#                       get_states=get_states,
#                       PID_params=PID_params,
#                       g0_params=g0_params,
#                       GMM_net=GMM_net,
#                       GMM_k=K)

#     return gen_params


def get_PID_priors(dim, vel):
    """Return a dictionary of PID controller parameters
    """
    with tf.variable_scope('PID_priors'):
        priors = {}

        priors['Kp'] = Gamma(
            np.ones(dim, np.float32) * 2, np.ones(dim, np.float32) * vel,
            name='Kp', value=np.ones(dim, np.float32) / vel)
        priors['Ki'] = Exponential(
            np.ones(dim, np.float32) / vel, name='Ki',
            value=np.zeros(dim, np.float32))
        priors['Kd'] = Exponential(
            np.ones(dim, np.float32), name='Kd',
            value=np.zeros(dim, np.float32))

        return priors


class Point_Mass(PointMass):
    def __init__(self, params, validate_args=True, allow_nan_stats=True,
                 name="PointMass"):
        super(Point_Mass, self).__init__(
            params=params, validate_args=validate_args,
            allow_nan_stats=allow_nan_stats, name=name)

    def _log_prob(self, value):
        return tf.zeros([])

    def _prob(self, value):
        return tf.zeros([])


def get_PID_posteriors(dim):
    with tf.variable_scope('PID_posteriors'):
        posteriors = {}

        unc_Kp = tf.Variable(tf.random_normal([dim], name='Kp_init_value'),
                             dtype=tf.float32, name='unc_Kp')
        unc_Ki = tf.Variable(tf.random_normal([dim], name='Ki_init_value'),
                             dtype=tf.float32, name='unc_Ki')
        unc_Kd = tf.Variable(tf.random_normal([dim], name='Kd_init_value'),
                             dtype=tf.float32, name='unc_Kd')
        posteriors['vars'] = ([unc_Kp] + [unc_Ki] + [unc_Kd])

        posteriors['Kp'] = Point_Mass(tf.nn.softplus(unc_Kp), name='Kp')
        posteriors['Ki'] = Point_Mass(tf.nn.softplus(unc_Ki), name='Ki')
        posteriors['Kd'] = Point_Mass(tf.nn.softplus(unc_Kd), name='Kd')

        return posteriors


def init_g0_params(dim, K):
    with tf.variable_scope('g0_params'):
        g0 = {}

        g0['K'] = K
        g0['mu'] = tf.Variable(
            tf.random_normal([K, dim], name='mu_init_value'),
            dtype=tf.float32, name='mu')
        g0['unc_lambda'] = tf.Variable(
            tf.random_normal([K, dim], name='lambda_init_value'),
            dtype=tf.float32, name='unc_lambda')
        g0['unc_w'] = tf.Variable(
            tf.ones([K], name='w_init_value'), dtype=tf.float32, name='unc_w')

        return g0


def generate_trial(goal_model, ctrl_model, y0=None, trial_len=100):
    with tf.name_scope('generate_trial'):
        dim = goal_model.dim
        vel = ctrl_model.max_vel

        with tf.name_scope('initialize'):
            if y0 is None:
                y0 = tf.zeros([dim], tf.float32)
            g = tf.reshape(goal_model.sample_g0(), [1, dim], name='g0')
            u = tf.zeros([1, dim], tf.float32, name='u0')
            prev_error = tf.zeros([dim], tf.float32, name='prev_error')
            prev2_error = tf.zeros([dim], tf.float32, name='prev2_error')
            y = tf.reshape(y0, [1, dim], name='y0')

        with tf.name_scope('propogate'):
            for t in range(trial_len - 1):
                if t == 0:
                    v_t = tf.zeros_like(y0, tf.float32, name='v_%s' % t)
                else:
                    v_t = tf.subtract(y[t], y[t - 1], name='v_%s' % t)
                s_t = tf.stack([y[t], v_t], 0, name='s_%s' % t)
                g_new = tf.reshape(goal_model.sample_GMM(s_t, g[t]), [1, dim],
                                   name='g_%s' % (t + 1))
                g = tf.concat([g, g_new], 0, name='concat_g_%s' % (t + 1))

                error = tf.subtract(g[t + 1], y[t], name='error_%s' % t)
                errors = tf.stack([error, prev_error, prev2_error], 0,
                                  name='errors_%s' % t)
                u_new = tf.reshape(ctrl_model.update_ctrl(errors, u[t]),
                                   [1, dim], name='u_%s' % (t + 1))
                u = tf.concat([u, u_new], 0, name='concat_u_%s' % (t + 1))

                prev2_error = prev_error
                prev_error = error

                y_new = tf.reshape(tf.clip_by_value(
                    y[t] + vel * tf.clip_by_value(
                        u[t + 1], -1., 1., name='clip_u_%s' % (t + 1)),
                    -1., 1., name='bound_y_%s' % (t + 1)),
                    [1, dim], name='y_%s' % (t + 1))
                y = tf.concat([y, y_new], 0, name='concat_y_%s' % (t + 1))

        return y, u, g


def batch_generator(arrays, batch_size, randomize=True):
    """Minibatch generator over one dataset of shape
    (#observations, #dimensions)
    """
    n_trials = len(arrays)
    n_batch = math.floor(n_trials / batch_size)
    if randomize:
        np.random.shuffle(arrays)

    start = 0
    while True:
        batches = []
        for _ in range(n_batch):
            stop = start + batch_size
            diff = stop - n_trials

            if diff <= 0:
                batch = np.array(arrays[start:stop])
                start = stop
            batches.append(batch)

        yield batches


def batch_generator_pad(arrays, batch_size, extra_conds=None, ctrl_obs=None,
                        randomize=True):
    n_trials = len(arrays)
    n_batch = math.floor(n_trials / batch_size)
    if randomize:
        p = np.random.permutation(n_trials)
        arrays = arrays[p]
        if extra_conds is not None:
            extra_conds = extra_conds[p]
        if ctrl_obs is not None:
            ctrl_obs = ctrl_obs[p]

    start = 0
    while True:
        batches = []
        conds = []
        ctrls = []
        for _ in range(n_batch):
            stop = start + batch_size
            diff = stop - n_trials

            if diff <= 0:
                batch = arrays[start:stop]
                if extra_conds is not None:
                    cond = np.array(extra_conds[start:stop])
                if ctrl_obs is not None:
                    ctrl = np.array(ctrl_obs[start:stop])
                start = stop
            batch = pad_batch(batch)
            batches.append(batch)
            if extra_conds is not None:
                conds.append(cond)
            if ctrl_obs is not None:
                ctrl = pad_batch(ctrl, mode='zero')
                ctrls.append(ctrl)

        yield batches, conds, ctrls


def pad_batch(arrays, mode='edge'):
    max_len = np.max([len(a) for a in arrays])
    if mode == 'edge':
        return np.array([np.pad(a, ((0, max_len - len(a)), (0, 0)),
                                'edge') for a in arrays])
    elif mode == 'zero':
        return np.array(
            [np.pad(a, ((0, max_len - len(a)), (0, 0)), 'constant',
                    constant_values=0) for a in arrays])


def pad_extra_conds(data, extra_conds=None):
    if extra_conds is not None:
        if not isinstance(extra_conds, tf.Tensor):
            extra_conds = tf.constant(extra_conds, dtype=tf.float32,
                                      name='extra_conds')
        extra_conds_repeat = tf.tile(
            tf.expand_dims(extra_conds, 1), [1, tf.shape(data)[1], 1],
            name='repeat_extra_conds')
        padded_data = tf.concat([data, extra_conds_repeat], axis=-1,
                                name='pad_extra_conds')
        return padded_data
    else:
        raise Exception('Must provide extra conditions.')


def add_summary(summary_op, inference, session, feed_dict, step):
    if inference.n_print != 0:
        if step == 1 or step % inference.n_print == 0:
            summary = session.run(summary_op, feed_dict=feed_dict)
            inference.train_writer.add_summary(summary, step)


class DatasetTrialIndexIterator(object):
    """Basic trial iterator
    """
    def __init__(self, y, randomize=False, batch_size=1):
        self.y = y
        self.randomize = randomize

    def __iter__(self):
        n_batches = len(self.y)
        if self.randomize:
            indices = list(range(n_batches))
            np.random.shuffle(indices)
            for i in indices:
                yield self.y[i]
        else:
            for i in range(n_batches):
                yield self.y[i]


class MultiDatasetTrialIndexIterator(object):
    """Trial iterator over multiple datasets of shape
    (ntrials, trial_len, trial_dimensions)
    """
    def __init__(self, data, randomize=False, batch_size=1):
        self.data = data
        self.randomize = randomize

    def __iter__(self):
        n_batches = len(self.data[0])
        if self.randomize:
            indices = list(range(n_batches))
            np.random.shuffle(indices)
            for i in indices:
                yield tuple(dset[i] for dset in self.data)
        else:
            for i in range(n_batches):
                yield tuple(dset[i] for dset in self.data)


class DataSetTrialIndexTF(object):
    """Tensor version of Minibatch iterator over one dataset of shape
    (nobservations, ndimensions)
    """
    def __init__(self, data, batch_size=100):
        self.data = data
        self.batch_size = batch_size

    def __iter__(self):
        new_data = [tf.constant(d) for d in self.data]
        data_iter_vb_new = tf.train.batch(new_data, self.batch_size,
                                          dynamic_pad=True)
        # data_iter_vb = [vb.eval() for vb in data_iter_vb_new]
        return iter(data_iter_vb_new)


class DatasetMiniBatchIterator(object):
    """Minibatch iterator over one dataset of shape
    (nobservations, ndimensions)
    """
    def __init__(self, data, batch_size, randomize=False):
        super(DatasetMiniBatchIterator, self).__init__()
        self.data = data  # tuple of datasets w/ same nobservations
        self.batch_size = batch_size
        self.randomize = randomize

    def __iter__(self):
        rows = range(len(self.data))
        if self.randomize:
            np.random.shuffle(rows)
        beg_indices = range(0, len(self.data) - self.batch_size + 1,
                            self.batch_size)
        end_indices = range(self.batch_size, len(self.data) + 1,
                            self.batch_size)
        for beg, end in zip(beg_indices, end_indices):
            curr_rows = rows[beg:end]
            yield self.data[curr_rows, :]


class MultiDatasetMiniBatchIterator(object):
    """Minibatch iterator over multiple datasets of shape
    (nobservations, ndimensions)
    """
    def __init__(self, data, batch_size, randomize=False):
        super(MultiDatasetMiniBatchIterator, self).__init__()
        self.data = data  # tuple of datasets w/ same nobservations
        self.batch_size = batch_size
        self.randomize = randomize

    def __iter__(self):
        rows = range(len(self.data[0]))
        if self.randomize:
            np.random.shuffle(rows)
        beg_indices = range(0, len(self.data[0]) - self.batch_size + 1,
                            self.batch_size)
        end_indices = range(self.batch_size, len(self.data[0]) + 1,
                            self.batch_size)
        for beg, end in zip(beg_indices, end_indices):
            curr_rows = rows[beg:end]
            yield tuple(dset[curr_rows, :] for dset in self.data)


# class hps_dict_to_obj(dict):
#     '''Helper class allowing us to access hps dictionary more easily.
#     '''
#     def __getattr__(self, key):
#         if key in self:
#             return self[key]
#         else:
#             assert False, ('%s does not exist.' % key)

#     def __setattr__(self, key, value):
#         self[key] = value


class KLqp_profile(ed.KLqp):
    def __init__(self, options=None, run_metadata=None, latent_vars=None,
                 data=None):
        super(KLqp_profile, self).__init__(latent_vars=latent_vars, data=data)
        self.options = options
        self.run_metadata = run_metadata

    def update(self, feed_dict=None):
        if feed_dict is None:
            feed_dict = {}

        for key, value in six.iteritems(self.data):
            if isinstance(key, tf.Tensor) and "Placeholder" in key.op.type:
                feed_dict[key] = value

        sess = ed.get_session()
        _, t, loss = sess.run([self.train, self.increment_t, self.loss],
                              options=self.options,
                              run_metadata=self.run_metadata,
                              feed_dict=feed_dict)

        if self.debug:
            sess.run(self.op_check, feed_dict)

        if self.logging and self.n_print != 0:
            if t == 1 or t % self.n_print == 0:
                summary = sess.run(self.summarize, feed_dict)
                self.train_writer.add_summary(summary, t)

        return {'t': t, 'loss': loss}
