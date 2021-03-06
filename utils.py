import math
import numpy as np
from scipy.stats import norm
from matplotlib.colors import Normalize
import tensorflow as tf
from tensorflow.contrib.distributions import bijectors, softplus_inverse
from tensorflow.contrib.keras import layers, models
from edward import KLqp
from edward.models import RandomVariable
from edward.util import get_session, get_variables, Progbar, transform
import six
import os
from datetime import datetime
from tf_gbds.layers import PKBiasLayer, PKRowBiasLayer


class set_cbar_zero(Normalize):
    """Normalize and center colormap at the desired value (midpoint).
    """

    def __init__(self, vmin=None, vmax=None, midpoint=0., clip=False):
        self.midpoint = midpoint
        Normalize.__init__(self, vmin, vmax, clip)

    def __call__(self, value, clip=None):
        x, y = ([min(self.vmin, -self.vmax), self.midpoint,
                 max(self.vmax, -self.vmin)], [0, 0.5, 1])
        return np.ma.masked_array(np.interp(value, x, y))


def gauss_convolve(x, sigma, pad_method="edge_pad"):
    """Smoothe with gaussian filter with the following padding methods:
        * edge_pad: pad with the values on the edges
        * extrapolate: extrapolate the end pad based on dx at the end
        * zero_pad: pad with zeros
    """
    method_types = ["edge_pad", "extrapolate", "zero_pad"]
    if pad_method not in method_types:
        raise Exception("Padding method not recognized")
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
    """Apply Gaussian convolution Smoothing method to real data.
    """
    rtrial = trial.copy()
    for i in range(rtrial.shape[1]):
        rtrial[:, i] = gauss_convolve(rtrial[:, i], sigma,
                                      pad_method=pad_method)
    return rtrial


# def gen_data(n_trials, n_obs, sigma=np.log1p(np.exp(-5. * np.ones((1, 2)))),
#              eps=np.log1p(np.exp(-10.)), Kp=1, Ki=0, Kd=0,
#              vel=1e-2 * np.ones((3))):
#     """Generate fake data to test the accuracy of the model.
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


def load_data(data_dir, hps):
    """ Load data from the given directory.
    """
    features = {"trajectory": tf.FixedLenFeature((), tf.string)}
    if hps.extra_conds:
        features.update({"extra_conds": tf.FixedLenFeature(
            (), tf.string)})
    if hps.ctrl_obs:
        features.update({"ctrl_obs": tf.FixedLenFeature(
            (), tf.string)})

    # the initial position
    y0 = tf.reshape([0., -0.58, 0.], [1, hps.obs_dim], "y0")

    def _read_data(example):
        parsed_features = tf.parse_single_example(example, features)
        trajectory = tf.concat(
            [y0, tf.reshape(
                tf.decode_raw(parsed_features["trajectory"], tf.float32),
                [-1, hps.obs_dim])], 0)
        data = (trajectory,)

        if "extra_conds" in parsed_features:
            extra_conds = tf.reshape(
                tf.decode_raw(parsed_features["extra_conds"], tf.float32),
                [hps.extra_dim])
            data += (extra_conds,)
        if "ctrl_obs" in parsed_features:
            ctrl_obs = tf.reshape(
                tf.decode_raw(parsed_features["ctrl_obs"], tf.float32),
                [-1, hps.obs_dim])
            data += (ctrl_obs,)

        return data

    # def _pad_data(batch):
    #     batch["trajectory"] = pad_batch(batch["trajectory"])
    #     if "ctrl_obs" in batch:
    #         batch["ctrl_obs"] = pad_batch(batch["ctrl_obs"],
    #                                       mode="zero")

    #     return batch

    with tf.name_scope("preprocessing"):
        dataset = tf.data.TFRecordDataset(data_dir)
        dataset = dataset.map(_read_data)
        dataset = dataset.shuffle(
            buffer_size=100000,
            seed=tf.random_uniform([], minval=-2**63+1, maxval=2**63-1,
                                   dtype=tf.int64))
        dataset = dataset.apply(
            tf.contrib.data.batch_and_drop_remainder(hps.B))
        # if hps.B > 1:
        #     dataset = dataset.map(_pad_data)
        iterator = dataset.make_initializable_iterator("iterator")

    return iterator


def get_max_velocities(data_dirs, dim):
    max_vel = np.zeros((dim), np.float32)
    n_trials = []

    feature = {"trajectory": tf.FixedLenFeature((), tf.string)}
    y0 = tf.reshape([0., -0.58, 0.], [1, dim])
    def _read_data(example):
        data_dict = tf.parse_single_example(example, feature)
        traj = tf.concat(
            [y0, tf.reshape(tf.decode_raw(
                data_dict["trajectory"], tf.float32), [-1, dim])], 0)

        return traj

    for data_dir in data_dirs:
        dataset = tf.data.TFRecordDataset(data_dir)
        dataset = dataset.map(_read_data)
        traj = dataset.make_one_shot_iterator().get_next()
        trial_max_vel = tf.reduce_max(tf.abs(traj[1:] - traj[:-1]), 0,
                                      name="trial_maximum_velocity")

        dataset_size = 0
        with tf.Session() as sess:
            while True:
                try:
                    max_vel = np.maximum(sess.run(trial_max_vel), max_vel)
                    dataset_size += 1
                except tf.errors.OutOfRangeError:
                    break
        n_trials.append(dataset_size)

    return np.around(max_vel, decimals=3) + 0.001, n_trials


def get_vel(traj, max_vel):
    """Input a time series of trajectory and compute velocity for each
    coordinate.
    """
    with tf.name_scope("get_velocity"):
        vel = tf.pad(
            tf.divide(traj[:, 1:] - traj[:, :-1], max_vel.astype(np.float32),
                      name="standardize"), [[0, 0], [1, 0], [0, 0]],
            name="pad_zero")
        states = tf.concat([traj, vel], -1, name="states")

        return states


def get_accel(traj, max_vel):
    """Input a time series of trajectory and compute velocity and
    acceleration for each coordinate.
    """
    with tf.name_scope("get_acceleration"):
        states = get_vel(traj, max_vel)
        accel = traj[:, 2:] - 2 * traj[1:-1] + traj[:-2]
        accel = tf.pad(accel, [[0, 0], [2, 0], [0, 0]], name="pad_zero")
        states = tf.concat([states, accel], -1, name="states")

        return states


def get_model_params(name, agents, obs_dim, state_dim, extra_dim,
                     gen_n_layers, gen_hidden_dim, GMM_K, PKLparams,
                     unc_sigma, sigma_trainable, sigma_penalty,
                     goal_boundaries, goal_boundary_penalty, latent_ctrl,
                     rec_lag, rec_n_layers, rec_hidden_dim, penalty_Q,
                     unc_epsilon, epsilon_trainable, epsilon_penalty,
                     clip, clip_range, clip_tolerance, clip_penalty, epoch):
    with tf.variable_scope("model_parameters"):
        priors = []

        for a in agents:
            with tf.variable_scope(a["name"]):
                if sigma_trainable:
                    unc_sigma_init = tf.Variable(
                        unc_sigma * np.ones((1, a["dim"]), np.float32),
                        name="unc_sigma")
                else:
                    unc_sigma_init = tf.constant(
                        unc_sigma * np.ones((1, a["dim"]), np.float32),
                        name="unc_sigma")
                if epsilon_trainable:
                    unc_eps_init = tf.Variable(
                        unc_epsilon * np.ones((1, a["dim"]), np.float32),
                        name="unc_epsilon")
                else:
                    unc_eps_init = tf.constant(
                        unc_epsilon * np.ones((1, a["dim"]), np.float32),
                        name="unc_epsilon")

                priors.append(dict(
                    name=a["name"], col=a["col"], dim=a["dim"],
                    g0=get_g0_params(a["dim"], GMM_K),
                    GMM_NN=get_network(
                        "goal_GMM", (state_dim + extra_dim),
                        (GMM_K * a["dim"] * 2 + GMM_K),
                        gen_hidden_dim, gen_n_layers, PKLparams)[0],
                    GMM_K=GMM_K,
                    unc_sigma=unc_sigma_init,
                    sigma_trainable=sigma_trainable, sigma_pen=sigma_penalty,
                    g_bounds=goal_boundaries,
                    g_bounds_pen=goal_boundary_penalty,
                    PID=get_PID_params(a["dim"], epoch),
                    unc_eps=unc_eps_init,
                    eps_trainable=epsilon_trainable, eps_pen=epsilon_penalty,
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
            g_q_params=g_q_params, u_q_params=u_q_params)

        return params


def get_rec_params(obs_dim, extra_dim, lag, n_layers, hidden_dim,
                   penalty_Q=None, PKLparams=None, name="recognition"):
    """Return a dictionary of parameters for recognition model.
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


def get_network(name, input_dim, output_dim, hidden_dim, num_layers,
                PKLparams=None, batchnorm=False, is_shooter=False,
                row_sparse=False, add_pklayers=False, filt_size=None):
    """Return a NN with the specified parameters and a list of PKBias layers.
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
                M.add(layers.Dense(output_dim, activation="linear",
                                   name="Dense_%s" % (i + 1)))
            else:
                M.add(layers.Dense(hidden_dim, activation="relu",
                                   name="Dense_%s" % (i + 1)))

        return M, PKbias_layers


def get_PID_params(dim, epoch):
    with tf.variable_scope("PID"):
        unc_Kp = tf.Variable(tf.multiply(
            softplus_inverse(1.), tf.ones(dim, tf.float32), "unc_Kp_init"),
                             name="unc_Kp")
        unc_Ki = tf.Variable(tf.multiply(
            softplus_inverse(1e-6), tf.ones(dim, tf.float32), "unc_Ki_init"),
                             name="unc_Ki")
        unc_Kd = tf.Variable(tf.multiply(
            softplus_inverse(1e-6), tf.ones(dim, tf.float32), "unc_Kd_init"),
                             name="unc_Kd")
        
        Kp = tf.nn.softplus(unc_Kp, "Kp")
        Ki = tf.nn.softplus(unc_Ki, "Ki")
        Kd = tf.nn.softplus(unc_Kd, "Kd")

        PID = {}
        PID["vars"] = [unc_Kp] + [unc_Ki] + [unc_Kd]
        PID["Kp"] = tf.cond(tf.greater(epoch, 30),
                            lambda: Kp, lambda: tf.stop_gradient(Kp))
        PID["Ki"] = tf.cond(tf.greater(epoch, 30),
                            lambda: Ki, lambda: tf.stop_gradient(Ki))
        PID["Kd"] = tf.cond(tf.greater(epoch, 30),
                            lambda: Kd, lambda: tf.stop_gradient(Kd))
        # PID["Kp"] = Kp
        # PID["Ki"] = Ki
        # PID["Kd"] = Kd

        return PID


def get_g0_params(dim, K):
    with tf.variable_scope("g0"):
        g0 = {}
        g0["K"] = K
        g0["mu"] = tf.Variable(
            tf.random_normal([K, dim], name="mu_init_value"),
            dtype=tf.float32, name="mu")
        g0["unc_lambda"] = tf.Variable(
            tf.random_normal([K, dim], name="lambda_init_value"),
            dtype=tf.float32, name="unc_lambda")
        g0["unc_w"] = tf.Variable(
            tf.ones([K], name="w_init_value"), dtype=tf.float32, name="unc_w")

        return g0


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
            tf.reshape(extra_conds, [1, 1, -1]),
            [tf.shape(data)[0], tf.shape(data)[1], 1],
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


class KLqp_profile(KLqp):
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

        sess = get_session()
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


class KLqp_clipgrads(KLqp):
    def __init__(self, *args, **kwargs):
        super(KLqp_clipgrads, self).__init__(*args, **kwargs)

    def initialize(self, n_iter=1000, n_print=None, scale=None,
                   auto_transform=True, logdir=None, log_timestamp=True,
                   log_vars=None, debug=False, optimizer=None, var_list=None,
                   use_prettytensor=False, global_step=None, n_samples=1,
                   kl_scaling=None, maxnorm=5.):

        if kl_scaling is None:
            kl_scaling = {}
        if n_samples <= 0:
            raise ValueError(
                "n_samples should be greater than zero: {}".format(n_samples))

        self.n_samples = n_samples
        self.kl_scaling = kl_scaling

        # from inference.py
        self.n_iter = n_iter
        if n_print is None:
            self.n_print = int(n_iter / 100)
        else:
            self.n_print = n_print

        self.progbar = Progbar(self.n_iter)
        self.t = tf.Variable(0, trainable=False, name="iteration")
        self.increment_t = self.t.assign_add(1)

        if scale is None:
            scale = {}
        elif not isinstance(scale, dict):
            raise TypeError("scale must be a dict object.")
        self.scale = scale

        self.transformations = {}
        if auto_transform:
            latent_vars = self.latent_vars.copy()
            self.latent_vars = {}
            self.latent_vars_unconstrained = {}
            for z, qz in six.iteritems(latent_vars):
                if hasattr(z, 'support') and hasattr(qz, 'support') and \
                        z.support != qz.support and qz.support != 'point':

                    z_unconstrained = transform(z)
                    self.transformations[z] = z_unconstrained

                    if qz.support == "points":
                        qz_unconstrained = qz
                    else:
                        qz_unconstrained = transform(qz)
                    self.latent_vars_unconstrained[
                        z_unconstrained] = qz_unconstrained

                    if z_unconstrained != z:
                        qz_constrained = transform(
                            qz_unconstrained,
                            bijectors.Invert(z_unconstrained.bijector))

                        try:
                            qz_constrained.params = \
                                    z_unconstrained.bijector.inverse(
                                        qz_unconstrained.params)
                        except:
                            pass
                    else:
                        qz_constrained = qz_unconstrained

                    self.latent_vars[z] = qz_constrained
                else:
                    self.latent_vars[z] = qz
                    self.latent_vars_unconstrained[z] = qz
            del latent_vars

        if logdir is not None:
            self.logging = True
            if log_timestamp:
                logdir = os.path.expanduser(logdir)
                logdir = os.path.join(
                    logdir,
                    datetime.strftime(datetime.utcnow(), "%Y%m%d_%H%M%S"))

            self._summary_key = tf.get_default_graph().unique_name(
                "summaries")
            self._set_log_variables(log_vars)
            self.train_writer = tf.summary.FileWriter(
                logdir, tf.get_default_graph())
        else:
            self.logging = False

        self.debug = debug
        if self.debug:
            self.op_check = tf.add_check_numerics_ops()

        self.reset = [tf.variables_initializer([self.t])]

        # from variational_inference.py
        if var_list is None:
            var_list = set()
            trainables = tf.trainable_variables()
            for z, qz in six.iteritems(self.latent_vars):
                var_list.update(get_variables(z, collection=trainables))
                var_list.update(get_variables(qz, collection=trainables))

            for x, qx in six.iteritems(self.data):
                if isinstance(x, RandomVariable) and \
                        not isinstance(qx, RandomVariable):
                    var_list.update(get_variables(x, collection=trainables))

        var_list = list(var_list)

        self.loss, grads_and_vars = self.build_loss_and_gradients(var_list)

        clipped_grads_and_vars = []
        for grad, var in grads_and_vars:
            if "kernel" in var.name or "bias" in var.name:
                clipped_grads_and_vars.append(
                    (tf.clip_by_norm(grad, maxnorm, axes=[0]), var))
            else:
                clipped_grads_and_vars.append((grad, var))
        # for grad, var in grads_and_vars:
        #     clipped_grads_and_vars.append(
        #         (tf.clip_by_value(grad, -1000., 1000.), var))
        del grads_and_vars

        if self.logging:
            tf.summary.scalar(
                "loss", self.loss, collections=[self._summary_key])
        for grad, var in clipped_grads_and_vars:
            tf.summary.histogram("gradient/" +
                                 var.name.replace(':', '/'),
                                 grad, collections=[self._summary_key])
            tf.summary.scalar("gradient_norm/" +
                              var.name.replace(':', '/'),
                              tf.norm(grad), collections=[self._summary_key])

        self.summarize = tf.summary.merge_all(key=self._summary_key)

        if optimizer is None and global_step is None:
            global_step = tf.Variable(0, trainable=False, name="global_step")

        if isinstance(global_step, tf.Variable):
            starter_learning_rate = 0.1
            learning_rate = tf.train.exponential_decay(
                starter_learning_rate, global_step, 100, 0.9, staircase=True)
        else:
            learning_rate = 0.01

        # Build optimizer.
        if optimizer is None:
            optimizer = tf.train.AdamOptimizer(learning_rate)
        elif isinstance(optimizer, str):
            if optimizer == 'gradientdescent':
                optimizer = tf.train.GradientDescentOptimizer(learning_rate)
            elif optimizer == 'adadelta':
                optimizer = tf.train.AdadeltaOptimizer(learning_rate)
            elif optimizer == 'adagrad':
                optimizer = tf.train.AdagradOptimizer(learning_rate)
            elif optimizer == 'momentum':
                optimizer = tf.train.MomentumOptimizer(learning_rate, 0.9)
            elif optimizer == 'adam':
                optimizer = tf.train.AdamOptimizer(learning_rate)
            elif optimizer == 'ftrl':
                optimizer = tf.train.FtrlOptimizer(learning_rate)
            elif optimizer == 'rmsprop':
                optimizer = tf.train.RMSPropOptimizer(learning_rate)
            else:
                raise ValueError('Optimizer class not found:', optimizer)
        elif not isinstance(optimizer, tf.train.Optimizer):
            raise TypeError(
                "Optimizer must be str, tf.train.Optimizer, or None.")

        with tf.variable_scope(None, default_name="optimizer") as scope:
            if not use_prettytensor:
                self.train = optimizer.apply_gradients(
                    clipped_grads_and_vars, global_step=global_step)
            else:
                import prettytensor as pt
                self.train = pt.apply_optimizer(
                    optimizer, losses=[self.loss],
                    global_step=global_step, var_list=var_list)

        self.reset.append(tf.variables_initializer(tf.get_collection(
            tf.GraphKeys.GLOBAL_VARIABLES, scope=scope.name)))
