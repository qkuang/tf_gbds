import tensorflow as tf
import math
from scipy.stats import norm
import numpy as np
from tensorflow.contrib.distributions import softplus_inverse
from tensorflow.contrib.keras import layers
from tensorflow.contrib.keras import models
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


def gauss_convolve(x, sigma, pad_method="edge_pad"):
    """Smoothing with gaussian convolution
    Pad Methods:
        * edge_pad: pad with the values on the edges
        * extrapolate: extrapolate the end pad based on dx at the end
        * zero_pad: pad with zeros
    """
    method_types = ["edge_pad", "extrapolate", "zero_pad"]
    if pad_method not in method_types:
        raise Exception("Pad method not recognized")
    edge = int(math.ceil(5 * sigma))
    fltr = norm.pdf(range(-edge, edge), loc=0, scale=sigma)
    fltr = fltr / sum(fltr)

    szx = x.size

    if pad_method == "edge_pad":
        buff = np.ones(edge)
        xx = np.append((buff * x[0]), x)
        xx = np.append(xx, (buff * x[-1]))
    elif pad_method == "extrapolate":
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

    y = np.convolve(xx, fltr, mode="valid")
    y = y[:szx]
    return y


def smooth_trial(trial, sigma=4.0, pad_method="extrapolate"):
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


def gen_data(n_trials, n_obs, sigma=1e-3 * np.ones((1, 3)),
             eps=1e-5, Kp=1, Ki=0, Kd=0,
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
    if hps.synthetic_data:
        trajs, goals = gen_data(
            n_trials=2000, n_obs=100, Kp=0.5, Ki=0.2, Kd=0.1)
        np.random.seed(hps.seed)  # set seed for consistent train/val split
        train_trajs = []
        val_trajs = []
        val_goals = []
        for (traj, goal) in zip(trajs, goals):
            if np.random.rand() <= hps.train_ratio:
                train_trajs.append(traj)
            else:
                val_trajs.append(traj)
                val_goals.append(goal)

        np.save(hps.model_dir + "/train_trajs", train_trajs)
        np.save(hps.model_dir + "/val_trajs", val_trajs)
        np.save(hps.model_dir + "/val_goals", val_goals)

        with tf.name_scope("load_training_set"):
            train_set = tf.data.Dataset.from_tensor_slices(
                [tf.convert_to_tensor(trial, tf.float32)
                 for trial in train_trajs])
            train_set = train_set.map(lambda x: {"trajectory": x})

    elif hps.data_dir is not None:
        features = {"trajectory": tf.FixedLenFeature((), tf.string)}
        if hps.extra_conds:
            # assume extra conditions are of type int64
            features.update({"extra_conds": tf.FixedLenFeature(
                (hps.extra_dim), tf.int64)})
        if hps.ctrl_obs:
            features.update({"ctrl_obs": tf.FixedLenFeature(
                (), tf.string)})

        def _read_data(example):
            parsed_features = tf.parse_single_example(example, features)
            entry = {}
            entry["trajectory"] = tf.reshape(
                tf.decode_raw(parsed_features["trajectory"], tf.float32),
                [-1, hps.obs_dim])

            if "extra_conds" in parsed_features:
                entry["extra_conds"] = tf.cast(
                    parsed_features["extra_conds"], tf.float32)
            if "ctrl_obs" in parsed_features:
                entry["ctrl_obs"] = tf.reshape(
                    tf.decode_raw(parsed_features["ctrl_obs"],
                                  tf.float32), [-1, hps.obs_dim])

            return entry

        # def _pad_data(batch):
        #     batch["trajectory"] = pad_batch(batch["trajectory"])
        #     if "ctrl_obs" in batch:
        #         batch["ctrl_obs"] = pad_batch(batch["ctrl_obs"],
        #                                       mode="zero")

        #     return batch

        with tf.name_scope("load_training_set"):
            if hps.data_dir.split(".")[-1] == "tfrecords":
                train_set = tf.data.TFRecordDataset(hps.data_dir)
                train_set = train_set.map(_read_data)
            else:
                raise Exception("Data format not recognized.")

    else:
        raise Exception("Data must be provided (either real or synthetic).")

    return train_set


# def get_max_velocities(datasets, dim):
#     """Get the maximium velocities from datasets
#     """
#     max_vel = [[] for _ in range(dim)]
#     for d in range(len(datasets)):
#         for i in range(len(datasets[d])):
#             for c in range(dim):
#                 if np.abs(np.diff(datasets[d][i][:, c])).max() > 0.001:
#                     max_vel[c].append(
#                         np.abs(np.diff(datasets[d][i][:, c])).max())

#     return np.array([max(vel) for vel in max_vel], np.float32)


def get_max_velocities(datasets, dim):
    max_vel = np.zeros((dim), np.float32)

    for dataset in datasets:
        traj = dataset.make_one_shot_iterator().get_next()["trajectory"]
        trial_max_vel = tf.reduce_max(tf.abs(traj[1:] - traj[:-1]), 0,
                                      name="trial_maximum_velocity")

        sess = tf.InteractiveSession()
        while True:
            try:
                max_vel = np.maximum(trial_max_vel.eval(), max_vel)
            except tf.errors.OutOfRangeError:
                break
        sess.close()

    return max_vel


def get_vel(traj, max_vel):
    """Input a time series of positions and include velocities for each
    coordinate in each row
    """
    with tf.name_scope("get_velocity"):
        vel = tf.pad(
            tf.divide(traj[:, 1:] - traj[:, :-1], max_vel.astype(np.float32),
                      name="standardize"), [[0, 0], [1, 0], [0, 0]],
            name="pad_zero")
        states = tf.concat([traj, vel], -1, name="states")

        return states


def get_accel(traj, max_vel):
    """Input a time series of positions and include velocities and acceleration
    for each coordinate in each row
    """
    with tf.name_scope("get_acceleration"):
        states = get_vel(traj, max_vel)
        accel = traj[:, 2:] - 2 * traj[1:-1] + traj[:-2]
        accel = tf.pad(accel, [[0, 0], [2, 0], [0, 0]], name="pad_zero")
        states = tf.concat([states, accel], -1, name="states")

        return states


def get_model_params(name, agents, obs_dim, state_dim, extra_dim,
                     gen_n_layers, gen_hidden_dim, GMM_K, PKLparams,
                     sigma, sigma_trainable,
                     goal_boundaries, goal_boundary_penalty,
                     all_vel, latent_ctrl,
                     rec_lag, rec_n_layers, rec_hidden_dim, penalty_Q,
                     # control_residual_tolerance, control_residual_penalty,
                     epsilon,
                     control_error_tolerance, control_error_penalty,
                     clip, clip_range, clip_tolerance, clip_penalty):

    with tf.variable_scope("model_parameters"):
        # PID_p = get_PID_priors(obs_dim, all_vel)
        PID_q = get_PID_posteriors(obs_dim, all_vel)

        priors = []
        for a in agents:
            priors.append(dict(
                name=a["name"], col=a["col"], dim=a["dim"],
                GMM_K=GMM_K,
                GMM_NN=get_network(
                    "%s_goal_GMM" % a["name"], (state_dim + extra_dim),
                    (GMM_K * a["dim"] * 2 + GMM_K),
                    gen_hidden_dim, gen_n_layers, PKLparams)[0],
                g0=get_g0_params(a["name"], a["dim"], GMM_K),
                sigma=sigma, sigma_trainable=sigma_trainable,
                g_bounds=goal_boundaries, g_bounds_pen=goal_boundary_penalty,
                PID=dict(Kp=tf.gather(PID_q["Kp"], a["col"],
                                      name="%s_Kp" % a["name"]),
                         Ki=tf.gather(PID_q["Ki"], a["col"],
                                      name="%s_Ki" % a["name"]),
                         Kd=tf.gather(PID_q["Kd"], a["col"],
                                      name="%s_Kd" % a["name"])),
                # u_res_tol=control_residual_tolerance,
                # u_res_pen=control_residual_penalty,
                eps=epsilon,
                u_error_tol=control_error_tolerance,
                u_error_pen=control_error_penalty,
                clip=clip, clip_range=clip_range, clip_tol=clip_tolerance,
                clip_pen=clip_penalty))

        g_q_params = get_rec_params(
            obs_dim, extra_dim, rec_lag, rec_n_layers,
            rec_hidden_dim, penalty_Q, PKLparams, "goal_posterior")

        if latent_ctrl:
            u_q_params = get_rec_params(
                obs_dim, extra_dim, rec_lag, rec_n_layers,
                rec_hidden_dim, penalty_Q, PKLparams, "control_posterior")
        else:
            u_q_params = None

        params = dict(
            name=name, obs_dim=obs_dim, agent_priors=priors,
            g_q_params=g_q_params,  # PID_p=PID_p,
            PID_q=PID_q, u_q_params=u_q_params)

        return params


def get_network(name, input_dim, output_dim, hidden_dim, num_layers,
                PKLparams=None, batchnorm=False, is_shooter=False,
                row_sparse=False, add_pklayers=False, filt_size=None):
    """Returns a NN with the specified parameters.
    Also returns a list of PKBias layers
    """

    with tf.variable_scope(name):
        M = models.Sequential(name=name)
        PKbias_layers = []
        M.add(layers.InputLayer(input_shape=(None, input_dim), name="Input"))
        if batchnorm:
            M.add(layers.BatchNormalization(name="BatchNorm"))
        if filt_size is not None:
            M.add(layers.ZeroPadding1D(padding=(filt_size - 1, 0),
                                       name="ZeroPadding"))
            M.add(layers.Conv1D(filters=hidden_dim, kernel_size=filt_size,
                                padding="valid", activation=tf.nn.relu,
                                name="Conv1D"))

        for i in range(num_layers):
            with tf.variable_scope("PK_Bias"):
                if is_shooter and add_pklayers:
                    if row_sparse:
                        PK_bias = PKRowBiasLayer(
                            M, PKLparams,
                            name="PKRowBias_%s" % (i + 1))
                    else:
                        PK_bias = PKBiasLayer(
                            M, PKLparams,
                            name="PKBias_%s" % (i + 1))
                    PKbias_layers.append(PK_bias)
                    M.add(PK_bias)

            if i == num_layers - 1:
                M.add(layers.Dense(
                    output_dim, activation="linear",
                    kernel_initializer=tf.random_normal_initializer(
                        stddev=0.1),
                    name="Dense_%s" % (i + 1)))
            else:
                M.add(layers.Dense(
                    hidden_dim, activation="relu",
                    kernel_initializer=tf.orthogonal_initializer(),
                    name="Dense_%s" % (i + 1)))

        return M, PKbias_layers


def get_rec_params(obs_dim, extra_dim, lag, n_layers, hidden_dim,
                   penalty_Q=None, PKLparams=None, name="recognition"):
    """Return a dictionary of timeseries-specific parameters for recognition
       model
    """

    with tf.variable_scope("%s_params" % name):
        Mu_net, PKbias_layers_mu = get_network(
            "Mu_NN", (obs_dim * (lag + 1) + extra_dim), obs_dim, hidden_dim,
            n_layers, PKLparams)
        Lambda_net, PKbias_layers_lambda = get_network(
            "Lambda_NN", obs_dim * (lag + 1) + extra_dim, obs_dim ** 2,
            hidden_dim, n_layers, PKLparams)
        LambdaX_net, PKbias_layers_lambdaX = get_network(
            "LambdaX_NN", obs_dim * (lag + 1) + extra_dim, obs_dim ** 2,
            hidden_dim, n_layers, PKLparams)

        dyn_params = dict(
            A=tf.Variable(
                .9 * np.eye(obs_dim), name="A", dtype=tf.float32),
            QinvChol=tf.Variable(
                np.eye(obs_dim), name="QinvChol", dtype=tf.float32),
            Q0invChol=tf.Variable(
                np.eye(obs_dim), name="Q0invChol", dtype=tf.float32))

        rec_params = dict(
            dyn_params=dyn_params,
            NN_Mu=dict(network=Mu_net,
                       PKbias_layers=PKbias_layers_mu),
            NN_Lambda=dict(network=Lambda_net,
                           PKbias_layers=PKbias_layers_lambda),
            NN_LambdaX=dict(network=LambdaX_net,
                            PKbias_layers=PKbias_layers_lambdaX),
            lag=lag)

        with tf.name_scope("penalty_Q"):
            if penalty_Q is not None:
                rec_params["p"] = penalty_Q

        return rec_params


def get_PID_priors(dim, vel):
    """Return a dictionary of PID controller parameters
    """
    with tf.variable_scope("PID_priors"):
        priors = {}

        priors["Kp"] = Gamma(np.ones(dim, np.float32) * 2,
                             np.ones(dim, np.float32) * vel, name="Kp")
        priors["Ki"] = Exponential(
            np.ones(dim, np.float32) / vel, name="Ki")
        priors["Kd"] = Exponential(
            np.ones(dim, np.float32), name="Kd")

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


def get_PID_posteriors(dim, vel):
    with tf.variable_scope("PID_posteriors"):
        posteriors = {}

        unc_Kp = tf.Variable(
            softplus_inverse(np.ones(dim, np.float32),
                             name="unc_Kp_init"),
            dtype=tf.float32, name="unc_Kp")
        unc_Ki = tf.Variable(
            softplus_inverse(np.ones(dim, np.float32) * 1e-3,
                             name="unc_Ki_init"),
            dtype=tf.float32, name="unc_Ki")
        unc_Kd = tf.Variable(
            softplus_inverse(np.ones(dim, np.float32) * 1e-3,
                             name="unc_Kd_init"),
            dtype=tf.float32, name="unc_Kd")
        posteriors["vars"] = [unc_Kp] + [unc_Ki] + [unc_Kd]

        posteriors["Kp"] = Point_Mass(tf.nn.softplus(unc_Kp), name="Kp")
        posteriors["Ki"] = Point_Mass(tf.nn.softplus(unc_Ki), name="Ki")
        posteriors["Kd"] = Point_Mass(tf.nn.softplus(unc_Kd), name="Kd")

        return posteriors


def get_g0_params(name, dim, K):
    with tf.variable_scope("%s_g0_params" % name):
        g0 = {}

        g0["K"] = K
        if dim == 1:
            g0["mu"] = tf.Variable(
                tf.random_normal([K, 1], name="mu_init_value"),
                dtype=tf.float32, name="mu")
        elif dim == 2:
            g0["mu"] = tf.Variable(
                tf.concat([tf.ones([K, 1]), tf.random_normal([K, 1])], 1,
                          name="mu_init_value"),
                dtype=tf.float32, name="mu")
        g0["unc_lambda"] = tf.Variable(
            tf.random_normal([K, dim], name="lambda_init_value"),
            dtype=tf.float32, name="unc_lambda")
        g0["unc_w"] = tf.Variable(
            tf.ones([K], name="w_init_value"), dtype=tf.float32, name="unc_w")

        return g0


# def batch_generator(arrays, batch_size, randomize=True):
#     n_trials = len(arrays)
#     n_batch = math.floor(n_trials / batch_size)
#     if randomize:
#         np.random.shuffle(arrays)

#     start = 0
#     while True:
#         batches = []
#         for _ in range(n_batch):
#             stop = start + batch_size
#             diff = stop - n_trials

#             if diff <= 0:
#                 batch = np.array(arrays[start:stop])
#                 start = stop
#             batches.append(batch)

#         yield batches


# def batch_generator_pad(arrays, batch_size, extra_conds=None, ctrl_obs=None,
#                         randomize=True):
#     n_trials = len(arrays)
#     if randomize:
#         p = np.random.permutation(n_trials)
#         arrays = np.array([arrays[i] for i in p])
#         if extra_conds is not None:
#             extra_conds = np.array([extra_conds[i] for i in p])
#         if ctrl_obs is not None:
#             ctrl_obs = np.array([ctrl_obs[i] for i in p])

#     n_batch = math.floor(n_trials / batch_size)
#     start = 0
#     while True:
#         batches = []
#         if extra_conds is not None:
#             conds = []
#         else:
#             conds = None
#         if ctrl_obs is not None:
#             ctrls = []
#         else:
#             ctrls = None

#         for _ in range(n_batch):
#             stop = start + batch_size
#             diff = stop - n_trials

#             if diff <= 0:
#                 batch = arrays[start:stop]
#                 if extra_conds is not None:
#                     cond = np.array(extra_conds[start:stop])
#                 if ctrl_obs is not None:
#                     ctrl = np.array(ctrl_obs[start:stop])
#                 start = stop

#             batch = pad_batch(batch)
#             batches.append(batch)
#             if extra_conds is not None:
#                 conds.append(cond)
#             if ctrl_obs is not None:
#                 ctrl = pad_batch(ctrl, mode="zero")
#                 ctrls.append(ctrl)

#         yield batches, conds, ctrls


# def pad_batch(arrays, mode="edge"):
#     max_len = np.max([len(a) for a in arrays])
#     if mode == "edge":
#         return np.array([np.pad(a, ((0, max_len - len(a)), (0, 0)),
#                                 "edge") for a in arrays])
#     elif mode == "zero":
#         return np.array(
#             [np.pad(a, ((0, max_len - len(a)), (0, 0)), "constant",
#                     constant_values=0) for a in arrays])


def pad_batch(batch, mode="edge"):
    max_len = tf.reduce_max(
        tf.map_fn(lambda x: tf.shape(x)[0], batch, dtype=tf.int32,
                  name="trial_length"), name="max_length")

    if mode == "edge":
        return tf.map_fn(
            lambda x: tf.concat(
                [x, tf.tile(tf.expand_dims(x[-1], 0),
                            [max_len - tf.shape(x)[0], 1])], 0), batch)
    elif mode == "zero":
        return tf.map_fn(
            lambda x: tf.pad(x, [[0, max_len - tf.shape(x)[0]], [0, 0]],
                             "constant"), batch)


def pad_extra_conds(data, extra_conds):
    if extra_conds is not None:
        extra_conds = tf.convert_to_tensor(extra_conds, dtype=tf.float32,
                                           name="extra_conds")
        extra_conds_repeat = tf.tile(
            tf.expand_dims(extra_conds, 1), [1, tf.shape(data)[1], 1],
            name="repeat_extra_conds")
        padded_data = tf.concat([data, extra_conds_repeat], axis=-1,
                                name="pad_extra_conds")

        return padded_data

    else:
        raise Exception("Must provide extra conditions.")


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
#     """Helper class allowing us to access hps dictionary more easily.
#     """
#     def __getattr__(self, key):
#         if key in self:
#             return self[key]
#         else:
#             assert False, ("%s does not exist." % key)

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

        return {"t": t, "loss": loss}


class KLqp_grad_clipnorm(ed.KLqp):
    def __init__(self, n_samples=1, kl_scaling=None, *args, **kwargs):
        super(KLqp_grad_clipnorm, self).__init__(*args, **kwargs)

    def initialize(self, var_list, optimizer, global_step=None,
                   maxnorm=5., *args, **kwargs):
        super(KLqp_grad_clipnorm, self).initialize(*args, **kwargs)

        self.loss, grads_and_vars = self.build_loss_and_gradients(var_list)

        for grad, var in grads_and_vars:
            if "kernel" in var.name or "bias" in var.name:
                grad = tf.clip_by_norm(grad, maxnorm, axes=[0])

        with tf.variable_scope(None, default_name="optimizer") as scope:
            self.train = optimizer.apply_gradients(grads_and_vars,
                                                   global_step=global_step)

        self.reset.append(tf.variables_initializer(
            tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES,
                              scope=scope.name)))
