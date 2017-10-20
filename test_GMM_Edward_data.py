import argparse
from tf_gbds.utils import *
# from tf_gbds.prep_GAN import prep_GAN_data, prep_injection_conditions
import sys
import os
from os.path import expanduser
import tensorflow as tf
from tensorflow.python.client import timeline
import numpy as np
import time
import tf_gbds.GenerativeModel_GMM_Edward as G
import tf_gbds.RecognitionModel_Edward as R
from edward import KLqp

# import pickle as pkl
# export LD_LIBRARY_PATH=/usr/local/cuda/extras/CUPTI/lib64:$LD_LIBRARY_PATH


def gen_data(n_trial, n_obs, sigma=np.log1p(np.exp(-5 * np.ones((1, 2)))),
             eps=1e-5, Kp=1, Ki=0, Kd=0,
             vel=1e-2 * np.ones((3))):
    p = []

    for s in range(n_trial):
        p_b = np.zeros((n_obs, 2), np.float32)
        p_g = np.zeros((n_obs, 1), np.float32)
        g = np.zeros(p_b.shape, np.float32)
        prev_error_b = 0
        prev_error_g = 0
        int_error_b = 0
        int_error_g = 0

        init = np.pi * (np.random.rand() * 2 - 1)
        g_mu_y = 0.75 * np.sin(1. * (np.linspace(0, 2 * np.pi, n_obs) - init))
        g_mu = np.hstack([np.ones((n_obs, 1)), g_mu_y.reshape(n_obs, 1)])
        g_lambda = np.array([16, 16], np.float32)
        g[0] = [1, 0.75 * (np.random.rand() * 2 - 1)]

        for t in range(n_obs - 1):
            g[t + 1] = (g[t] + g_lambda * g_mu[t + 1]) / (1 + g_lambda)
            var = sigma ** 2 / (1 + g_lambda)
            g[t + 1] += (np.random.randn(1, 2) * np.sqrt(var)).reshape(2,)

            error_b = g[t + 1] - p_b[t]
            int_error_b += error_b
            der_error_b = error_b - prev_error_b
            u_b = Kp * error_b + Ki * int_error_b + Kd * der_error_b + eps * np.random.randn(2,)
            prev_error_b = error_b
            p_b[t + 1] = p_b[t] + vel[1:] * np.clip(u_b, -1, 1)

            error_g = p_b[t + 1, 1] - p_g[t]
            int_error_g += error_g
            der_error_g = error_g - prev_error_g
            u_g = Kp * error_g + Ki * int_error_g + Kd * der_error_g + eps * np.random.randn()
            prev_error_g = error_g
            p_g[t + 1] = p_g[t] + vel[0] * np.clip(u_g, -1, 1)

        p.append(np.hstack([p_g, p_b]))

    return p


def run_model(**kwargs):
    outname = kwargs['outname']
    seed = kwargs['seed']
    # max_sessions = kwargs['max_sessions']
    # session_type = kwargs['session_type']
    # session_index = kwargs['session_index']
    # data_loc = kwargs['data_loc']
    rec_lag = kwargs['rec_lag']
    rec_lag = 0
    nlayers_rec = kwargs['nlayers_rec']
    hidden_dim_rec = kwargs['hidden_dim_rec']
    nlayers_gen = kwargs['nlayers_gen']
    hidden_dim_gen = kwargs['hidden_dim_gen']
    gmm_k = kwargs['gmm_k']
    add_accel = kwargs['add_accel']
    penalty_eps = kwargs['penalty_eps']
    penalty_sigma = kwargs['penalty_sigma']
    # model class allows for having 2 boundaries with different penalties,
    # but we later found that unnecessary, so the CLI only takes on penalty.
    # We left the possibility for 2 penalties in the model class just in case
    # it may be useful on a different dataset/task
    boundaries_g = (kwargs['boundary_g'], 2.0)
    penalty_g = (kwargs['penalty_g'], None)
    learning_rate = tf.cast(kwargs['learning_rate'], tf.float32)
    n_epochs = kwargs['n_epochs']

    np.random.seed(seed)  # set seed for consistent train/val split

    if not os.path.exists(outname):
        os.makedirs(outname)
    # if session_type == 'recording':
    #     print 'Loading movement data from recording sessions...'
    #     groups = get_session_names(session_index,
    #                                ('type',), ('recording',))
    # elif session_type == 'injection':
    #     print 'Loading movement data from injection sessions...'
    #     groups = get_session_names(session_index,
    #                                ('type', 'type'),
    #                                ('saline', 'muscimol'),
    #                                comb=np.any)
    # sys.stdout.flush()
    # groups = groups[-max_sessions:]
    # y_data, y_data_modes, y_val_data, y_val_data_modes = load_pk_data(data_loc,
    #                                                                   groups)
    # data = gen_data(n_trial=100, n_obs=200, Kp=2, Ki=0.1, Kd=0.5)
    data = gen_data(n_trial=500, n_obs=101, Kp=0.8, Ki=0.4, Kd=0.2)
    train_data = []
    val_data = []
    for trial in data:
        if np.random.rand() <= 0.85:
            train_data.append(trial)
        else:
            val_data.append(trial)
    np.save(outname + '/train_data', train_data)
    np.save(outname + '/val_data', val_data)

    yCols_goalie = [0]
    yCols_ball = [1, 2]

    obs_dim_g = len(yCols_goalie)
    obs_dim_b = len(yCols_ball)
    obs_dim = obs_dim_g + obs_dim_b

    # No CLI arguments for these bc no longer being used, but left just in case
    penalty_Q = None
    PKLparams = None
    row_sparse = False
    add_pklayers = False

    vel = get_max_velocities(train_data, val_data)
    ntrials = len(train_data)

    with tf.name_scope('model_setup'):
        with tf.name_scope('rec_params'):
            rec_params_u = get_rec_params_GBDS(seed, obs_dim, rec_lag, nlayers_rec,
                                             hidden_dim_rec, penalty_Q,
                                             PKLparams, 'u')
            rec_params_g = get_rec_params_GBDS(seed, obs_dim, rec_lag, nlayers_rec,
                                             hidden_dim_rec, penalty_Q,
                                             PKLparams, 'g')

        with tf.name_scope('gen_params'):
            with tf.name_scope('gen_goalie_params'):
                gen_params_goalie = get_gen_params_GBDS_GMM(seed, obs_dim_g, obs_dim, add_accel,
                                                   yCols_goalie, nlayers_gen, hidden_dim_gen, gmm_k, PKLparams, vel,
                                                   penalty_eps, penalty_sigma,
                                                   boundaries_g, penalty_g, 'goalie')

            with tf.name_scope('gen_ball_params'):
                gen_params_ball = get_gen_params_GBDS_GMM(seed, obs_dim_b, obs_dim, add_accel,
                                                   yCols_ball, nlayers_gen, hidden_dim_gen, gmm_k,
                                                   PKLparams, vel, penalty_eps,
                                                   penalty_sigma, boundaries_g, penalty_g, 'ball')

        # model = SGVB_GBDS(gen_params_b, gen_params_g, yCols_ball, yCols_goalie,
        #                   rec_params, ntrials)
        
        Y = tf.placeholder(tf.float32, shape=(None, None), name='Y')
        yDim_goalie = len(yCols_goalie)
        yDim_ball = len(yCols_ball)
        yDim = tf.shape(Y)[1]
        xDim = yDim

        # with tf.name_scope('train_gen_goalie_g'):
        #     g_goalie = G.GBDS_g(gen_params_goalie, yDim_goalie, yDim, Y, value = tf.zeros([tf.shape(Y)[0],yDim_goalie]))
        # with tf.name_scope('train_gen_goalie_u'):  
        #     u_goalie = G.GBDS_u(gen_params_goalie, g_goalie, Y, yDim_goalie, value = tf.zeros([tf.shape(Y)[0],yDim_goalie]))
        
        # with tf.name_scope('train_gen_ball_g'):          
        #     g_ball = G.GBDS_g(gen_params_ball, yDim_ball, yDim, Y, value = tf.zeros([tf.shape(Y)[0],yDim_ball]))
        # with tf.name_scope('train_gen_ball_u'):       
        #     u_ball = G.GBDS_u(gen_params_ball, g_ball, Y, yDim_ball, value = tf.zeros([tf.shape(Y)[0],yDim_ball]))

        with tf.name_scope('train_gen_g'):
            g = G.GBDS_g_all(gen_params_goalie, gen_params_ball, yDim, Y, value = tf.zeros([tf.shape(Y)[0],yDim]))
        with tf.name_scope('train_gen_u'):  
            u = G.GBDS_u_all(gen_params_goalie, gen_params_ball, g, Y, yDim, value = tf.zeros([tf.shape(Y)[0],yDim]))

        with tf.name_scope('train_rec_g'):
            qg = R.SmoothingLDSTimeSeries(rec_params_g, Y, xDim, yDim, value = tf.zeros([tf.shape(Y)[0],yDim]))
        with tf.name_scope('train_rec_u'):
            qu = R.SmoothingLDSTimeSeries(rec_params_u, Y, xDim, yDim, value = tf.zeros([tf.shape(Y)[0],yDim]))
            
        Y_pred = (Y + tf.reshape(vel, [1, yDim]) * tf.tanh(u))


    print('--------------Generative Params----------------')
    if penalty_eps is not None:
        print('Penalty on control signal noise, epsilon (Generative): %i' % penalty_eps)
    if penalty_sigma is not None:
        print('Penalty on goal state noise, sigma (Generative): %i' % penalty_sigma)
    if penalty_g[0] is not None:
        print('Penalty on goal state leaving boundary 1 (Generative): %i' % penalty_g[0])
    if penalty_g[1] is not None:
        print('Penalty on goal state leaving boundary 2 (Generative): %i' % penalty_g[1])
    print('--------------Recognition Params---------------')
    print('Num Layers (VILDS recognition): %i' % nlayers_rec)
    print('Hidden Dims (VILDS recognition): %i' % hidden_dim_rec)
    print('Input lag (VILDS recognition): %i' % rec_lag)
    # sys.stdout.flush()

    # set up an iterator over our training data
    data_iter_vb = DatasetTrialIndexIterator(train_data, randomize=True)
    n_iter_per_epoch = ntrials
    # data_iter_vb = DatasetMiniBatchIterator(train_data, batch_size=50, randomize=True)

    val_costs = []
    #ctrl_cost = []

    print('Setting up VB model...')
    # sys.stdout.flush()
    # ctrl_train_fn, ctrl_test_fn = (
    #     compile_functions_vb_training(model, learning_rate,
    #                                   add_pklayers=add_pklayers,
    #                                   mode=COMPILE_MODE))
    # if add_pklayers:
    #     model.mode = tf.placeholder(tf.float32, name='batch_mode')
    # if joint_spikes and model.isTrainingSpikeModel:
    #     model.spikes = tf.placeholder(tf.float32, name='batch_spikes')
    #     model.signals = tf.placeholder(tf.float32, name='batch_signals')

    # cap_noise = False

    summary_op = tf.summary.merge_all()

    # run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
    # run_metadata = tf.RunMetadata()

    saver = tf.train.Saver()

    # Iterate over the training data for the specified number of epochs
    with tf.Session(config=tf.ConfigProto(device_count={'GPU': 0})) as sess:
    # with tf.Session() as sess:
        train_writer = tf.summary.FileWriter(outname, sess.graph)
        
        inference = KLqp({g:qg, u:qu}, data={Y_pred: Y})
        inference.initialize(var_list=g.getParams() + u.getParams() +qg.getParams() +qu.getParams(),
          optimizer=tf.train.AdamOptimizer(learning_rate))
        tf.global_variables_initializer().run()

        print('---> Training control model')
        # sys.stdout.flush()
        for ie in range(n_epochs):
            print('--> entering epoch %i' % (ie + 1))

            avg_loss = 0.0

            for data in data_iter_vb:
                info_dict = inference.update(feed_dict = {Y:data})
                avg_loss += info_dict['loss']
                # inference.print_progress(info_dict)
            avg_loss = avg_loss / n_iter_per_epoch
            
            print("-log p(x) <= {:0.3f}".format(avg_loss))

            curr_val_cost = 0
            for i in range(len(val_data)):
                curr_val_cost += sess.run(inference.build_loss_and_gradients(
                    var_list=g.getParams() + u.getParams() + qg.getParams() + qu.getParams())[0],
                    feed_dict={Y: val_data[i]})
            val_costs.append(np.array(curr_val_cost / len(val_data)))
            print('Validation set cost: %f' % val_costs[-1])
            np.save(outname + '/val_costs', val_costs)

        train_writer.close()



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--outname', type=str, default='model_saved',
                        help='Name for model directory')
    parser.add_argument('--seed', type=int, default=1234,
                        help='Random seed')
    # parser.add_argument('--max_sessions', type=int, default=10,
    #                     help='Max number of sessions to load')
    # parser.add_argument('--session_type', default='recording', type=str,
    #                     choices=['recording', 'injection'],
    #                     help='Type of data session. Either neural recording ' +
    #                          'session or saline/muscimol injection session')
    # parser.add_argument('--session_index', type=str,
    #                     default='~/data/penaltykick/model_data/session_index.csv',
    #                     help='Location of session index file')
    # parser.add_argument('--data_loc', type=str,
    #                     default='~/data/penaltykick/model_data/compiled_penalty_kick_wspikes_wgaze_resplined.hdf5',
    #                     help='Location of data file')
    parser.add_argument('--rec_lag', type=int, default=10,
                        help='Number of timepoints to include as input to recognition model')
    parser.add_argument('--nlayers_rec', type=int, default=3,
                        help='Number of layers in recognition model NNs')
    parser.add_argument('--hidden_dim_rec', type=int, default=25,
                        help='Number of hidden units in recognition model NNs')
    parser.add_argument('--nlayers_gen', type=int, default=3,
                        help='Number of layers in generative model NNs')
    parser.add_argument('--hidden_dim_gen', type=int, default=64,
                        help='Number of hidden units in generative model NNs')
    parser.add_argument('--gmm_k', type=int, default=8,
                        help='Number of Gaussian components in GMM')
    parser.add_argument('--add_accel', action='store_true',
                        help='Add acceleration to states')
    parser.add_argument('--penalty_eps', type=float, default=None,
                        help='Penalty on epsilon (control signal noise)')
    parser.add_argument('--penalty_sigma', type=float, default=None,
                        help='Penalty on sigma (goal state noise)')
    parser.add_argument('--penalty_g', type=float, default=None,
                        help='Penalty for goal states escaping boundary_g.')
    parser.add_argument('--boundary_g', type=float, default=1.0,
                        help='Goal state boundary that corresponds to penalty')
    parser.add_argument('--learning_rate', type=float, default=1e-3,
                        help='Learning rate for adam optimizer')
    parser.add_argument('--n_epochs', type=int, default=100,
                        help='Number of iterations through the full training set')
    args = parser.parse_args()

    run_model(**vars(args))