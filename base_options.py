import argparse
import os
import sys
import torch
import datasets
import utils
import yaml
import time
import math
import numpy as np
import random
import fcntl
import atexit
import torch.distributed as dist
from contextlib import contextmanager
from torchtext import data


class State(object):
    class UniqueNamespace(argparse.Namespace):
        def __init__(self, requires_unique=True):
            self.__requires_unique = requires_unique
            self.__set_value = {}

        def requires_unique(self):
            return self.__requires_unique

        def mark_set(self, name, value):
            if self.__requires_unique and name in self.__set_value:
                raise argparse.ArgumentTypeError(
                    "'{}' appears several times: {}, {}.".format(
                        name, self.__set_value[name], value))
            self.__set_value[name] = value

    __inited = False

    def __init__(self, opt=None):
        if opt is None:
            self.opt = UniqueNamespace()
        else:
            if isinstance(opt, argparse.Namespace):
                opt = vars(opt)
            self.opt = argparse.Namespace(**opt)
        self.extras = {}
        self.__inited = True
        self._output_flag = True

    def __setattr__(self, k, v):
        if not self.__inited:
            return super(State, self).__setattr__(k, v)
        else:
            self.extras[k] = v

    def __getattr__(self, k):
        if k in self.extras:
            return self.extras[k]
        elif k in self.opt:
            return getattr(self.opt, k)
        raise AttributeError(k)

    def copy(self):
        return argparse.Namespace(**self.merge())

    def get_output_flag(self):
        return self._output_flag

    @contextmanager
    def pretend(self, **kwargs):
        saved = {}
        for key, val in kwargs.items():
            if key in self.extras:
                saved[key] = self.extras[key]
            setattr(self, key, val)
        yield
        for key, val in kwargs.items():
            self.pop(key)
            if key in saved:
                self.extras[key] = saved[key]

    def set_output_flag(self, val):
        self._output_flag = val

    def pop(self, k, default=None):
        return self.extras.pop(k, default)

    def clear(self):
        self.extras.clear()

    # returns a single dict containing both opt and extras
    def merge(self, public_only=False):
        vs = vars(self.opt).copy()
        vs.update(self.extras)
        if public_only:
            for k in tuple(vs.keys()):
                if k.startswith('_'):
                    vs.pop(k)
        return vs

    def get_base_directory(self):
        vs = self.merge()
        opt = argparse.Namespace(**vs)
        if opt.expr_name_format is not None:
            assert len(self.expr_name_format) > 0
            dirs = [fmt.format(**vs) for fmt in opt.expr_name_format]
        else:
            if opt.train_nets_type != 'loaded':
                train_nets_str = '{},{}'.format(opt.init, opt.init_param)


            name = 'arch({},{})_distillLR{}_E({},{},{})_lr{}_B{}x{}x{}'.format(
                opt.arch, train_nets_str, str(opt.distill_lr),
                opt.epochs, opt.decay_epochs, str(opt.decay_factor), str(opt.lr),
                opt.distilled_images_per_class_per_step, opt.distill_steps, opt.distill_epochs)
            if opt.sample_n_nets > 1:
                name += '_{}nets'.format(opt.sample_n_nets)
            name += '_train({})'.format(opt.train_nets_type)
            if opt.dropout:
                name += '_dropout'
            dirs = [opt.mode, opt.dataset, name]
        return os.path.join(opt.results_dir, *dirs)

    def get_load_directory(self):
        return self.get_base_directory()

    def get_save_directory(self):
        base_dir = self.get_base_directory()
        
        return base_dir

    def get_test_subdirectory(self):
        if self.test_name_format is not None:
            assert len(self.test_name_format) > 0
            vs = self.merge()
            return self.test_name_format.format(**vs)
        else:
            return 'nRun{}_nNet{}_nEpoch{}_image_{}_lr_{}{}'.format(
                self.test_n_runs, self.test_n_nets, self.test_distill_epochs,
                self.test_distilled_images, self.test_distilled_lrs[0],
                '' if len(self.test_distilled_lrs) == 1 else '({})'.format('_'.join(self.test_distilled_lrs[1:])))

    def get_model_dir(self):
        vs = vars(self.opt).copy()
        vs.update(self.extras)
        opt = argparse.Namespace(**vs)
        model_dir = opt.model_dir
        arch = opt.arch
        dataset = opt.dataset
        if self.model_subdir_format is not None and self.model_subdir_format != '':
            subdir = self.model_subdir_format.format(**vs)
        else:
            subdir = os.path.join('{:s}_{:s}_{:s}_{}'.format(dataset, arch, opt.init, opt.init_param))
        return os.path.join(model_dir, subdir, opt.phase)


class BaseOptions(object):
    def __init__(self):
        # argparse utils

        def comp(type, op, ref):
            op = getattr(type, '__{}__'.format(op))

            def check(value):
                ivalue = type(value)
                if not op(ivalue, ref):
                    raise argparse.ArgumentTypeError("expected value {} {}, but got {}".format(op, ref, value))
                return ivalue

            return check

        def int_gt(i):
            return comp(int, 'gt', i)

        def float_gt(i):
            return comp(float, 'gt', i)

        pos_int = int_gt(0)
        nonneg_int = int_gt(-1)
        pos_float = float_gt(0)

        def get_unique_action_cls(actual_action_cls):
            class UniqueSetAttrAction(argparse.Action):
                def __init__(self, *args, **kwargs):
                    self.subaction = actual_action_cls(*args, **kwargs)

                def __call__(self, parser, namespace, values, option_string=None):
                    if isinstance(namespace, State.UniqueNamespace):
                        requires_unique = namespace.requires_unique()
                    else:
                        requires_unique = False
                    if requires_unique:
                        namespace.mark_set(self.subaction.dest, values)
                    self.subaction(parser, namespace, values, option_string)

                def __getattr__(self, name):
                    return getattr(self.subaction, name)

            return UniqueSetAttrAction

        self.parser = parser = argparse.ArgumentParser(description='PyTorch Dataset Distillation')

        action_registry = parser._registries['action']
        for name, action_cls in action_registry.items():
            action_registry[name] = get_unique_action_cls(action_cls)

        parser.add_argument('--batch_size', type=pos_int, default=1024,
                            help='input batch size for training (default: 1024)')
        parser.add_argument('--test_batch_size', type=pos_int, default=1024,
                            help='input batch size for testing (default: 1024)')
        parser.add_argument('--test_niter', type=pos_int, default=1,
                            help='max number of batches to test (default: 1)')
        parser.add_argument('--epochs', type=pos_int, default=400, metavar='N',
                            help='number of total epochs to train (default: 400)')
        parser.add_argument('--decay_epochs', type=pos_int, default=40, metavar='N',
                            help='period of weight decay (default: 40)')
        parser.add_argument('--decay_factor', type=pos_float, default=0.5, metavar='N',
                            help='weight decay multiplicative factor (default: 0.1)')
        parser.add_argument('--lr', type=pos_float, default=0.01, metavar='LR',
                            help='learning rate used to actually learn stuff (default: 0.01)')
        parser.add_argument('--init', type=str, default='xavier',
                            help='network initialization [normal|xavier|kaiming|orthogonal|zero|default]')
        parser.add_argument('--init_param', type=float, default=1.,
                            help='network initialization param: gain, std, etc.')
        parser.add_argument('--base_seed', type=int, default=1, metavar='S',
                            help='base random seed (default: 1)')
        parser.add_argument('--log_interval', type=int, default=100, metavar='N',
                            help='how many batches to wait before logging training status')
        parser.add_argument('--checkpoint_interval', type=int, default=10, metavar='N',
                            help='checkpoint interval (epoch)')
        parser.add_argument('--dataset', type=str, default='imdb',
                            help='dataset: MNIST | Cifar10 | PASCAL_VOC | CUB200')
        parser.add_argument('--source_dataset', type=str, default=None,
                            help='dataset: MNIST | Cifar10 | PASCAL_VOC | CUB200')
        parser.add_argument('--dataset_root', type=str, default=None,
                            help='dataset root')
        parser.add_argument('--results_dir', type=str, default='./results/',
                            help='results directory')
        parser.add_argument('--arch', type=str, default='LeNet',
                            help='architecture: LeNet | AlexNet | etc.')
        parser.add_argument('--mode', type=str, default='distill_basic',
                            help='mode: train | distill_basic | distill_attack | distill_adapt ')
        parser.add_argument('--distill_lr', type=float, default=0.02,
                            help='learning rate to perform GD with distilled images PER STEP (default: 0.02)')
        parser.add_argument('--model_dir', type=str, default='./models/',
                            help='directory storing trained models')
        parser.add_argument('--model_subdir_format', type=str, default=None,
                            help='directory storing trained models')
        parser.add_argument('--train_nets_type', type=str, default='known_init',
                            help='[ unknown_init | known_init | loaded ]')  # add things like P(reset) = 0.7?
        parser.add_argument('--test_nets_type', type=str, default='same_as_train',
                            help='[ unknown_init | same_as_train | loaded ]')
        parser.add_argument('--dropout', action='store_true',
                            help='if set, use dropout')
        parser.add_argument('--distilled_images_per_class_per_step', type=pos_int, default=1,
                            help='use #batch_size distilled images for each class in each step')
        parser.add_argument('--distill_steps', type=pos_int, default=1,
                            help='Iterative distillation, use #num_steps * #batch_size * #classes distilled images. '
                                 'See also --distill_epochs. The total number '
                                 'of steps is distill_steps * distill_epochs.')
        parser.add_argument('--distill_epochs', type=pos_int, default=1,
                            help='how many times to repeat all steps 1, 2, 3, 1, 2, 3, ...')
        parser.add_argument('--n_nets', type=int, default=1,
                            help='# random nets')
        parser.add_argument('--sample_n_nets', type=pos_int, default=None,
                            help='sample # nets for each iteration. Default: equal to n_nets')
        parser.add_argument('--device_id', type=comp(int, 'ge', -1), default=0, help='device id, -1 is cpu')
        parser.add_argument('--image_dpi', type=pos_int, default=80,
                            help='dpi for visual image generation')
        parser.add_argument('--attack_class', type=nonneg_int, default=0,
                            help='when mode is distill_attack, the objective is to predict this class as target_class')
        parser.add_argument('--target_class', type=nonneg_int, default=1,
                            help='when mode is distill_attack, the objective is to predict forget class as this class')
        parser.add_argument('--expr_name_format', nargs='+', default=None, type=str,
                            help='expriment save dir name format. multiple values means nested folders')
        parser.add_argument('--phase', type=str, default='train',
                            help='phase')
        parser.add_argument('--test_distill_epochs', nargs='?', type=pos_int, default=None,
                            help='IN TEST, how many times to repeat all steps 1, 2, 3, 1, 2, 3, ...'
                                 'Defaults to distill_epochs.')
        parser.add_argument('--test_n_runs', type=pos_int, default=1,
                            help='do num test (no training), each test generates new distilled image, label, and lr')
        parser.add_argument('--test_n_nets', type=pos_int, default=1,
                            help='# reset model in test to get average performance, useful with unknown init')
        parser.add_argument('--test_distilled_images', default='loaded', type=str,
                            help='which distilled images to test [ loaded | random_train | kmeans_train ]')
        parser.add_argument('--test_distilled_lrs', default=['loaded'], nargs='+', type=str,
                            help='which distilled lrs to test [ loaded | fix [lr] | nearest_neighbor [k] [p] ]')
        parser.add_argument('--test_optimize_n_runs', default=None, type=pos_int,
                            help='if set, evaluate test_optimize_n_runs sets of test images, label and lr on '
                                 'test_optimize_n_nets training networks, and pick the best test_n_runs sets.'
                                 'Default: None.')
        parser.add_argument('--test_optimize_n_nets', default=20, type=pos_int,
                            help='number of networks used to optimize data. See doc for test_optimize_n_runs.')
        parser.add_argument('--num_workers', type=nonneg_int, default=8,
                            help='number of data loader workers')
        parser.add_argument('--no_log', action='store_true',
                            help='if set, will not log into file')
        parser.add_argument('--log_level', type=str, default='INFO',
                            help='logging level, e.g., DEBUG, INFO, WARNING, ERROR, CRITICAL')
        parser.add_argument('--test_name_format', nargs='+', type=str, default=None,
                            help='test save subdir name format. multiple values means nested folders')
        parser.add_argument('--world_size', nargs='?', type=comp(int, 'ge', 1), default=1,
                            help='if > 1, word size used for distributed training in reverse mode with NCCL. '
                                 'This will read an environ variable representing the process RANK, and several '
                                 'others needed to initialize the process group, which can '
                                 'be either MASTER_PORT & MASTER_ADDR, or INIT_FILE. '
                                 'Then it stores the values in state as "distributed_master_addr", '
                                 '"distributed_master_port", etc. Only rank 0 process writes checkpoints. ')
        parser.add_argument('--static_labels', type=int, default=1, help='0 for fixed labels during training, 1 for them to be learned as well.')
        parser.add_argument('--random_init_labels', type=str, default='', help=' "" for user-set labels init, other strings for special inits.')
        parser.add_argument('--num_distill_classes', type=int, default=None, help='Number of distill samples per step (can be less than number of classes.')
        parser.add_argument('--init_labels', type=int, nargs="*", default=None, help='If not random_init_labels, use this to set initial values of distill labels.')
        parser.add_argument('--textdata', type=bool, default=True, help='Is the dataset text-based?')
        parser.add_argument('--ntoken', type=int, default=5000, help='Number of possible unique words for text data')
        parser.add_argument('--ninp', type=int, default=50, help='Embedding size for text data')
        parser.add_argument('--maxlen', type=int, default=10, help='maxlen for text data')
        parser.add_argument('--learnable_embedding', type=bool, default=False, help='Should text embedding be learnable?')
        parser.add_argument('--reproduction_test', type=bool, default=False, help='Use original loss function instead of custom one?')
        parser.add_argument('--label_softmax', type=bool, default=False, help='Should softmax be applied to distillation labels in loss function?')
        parser.add_argument('--visualize', type=bool, default=True, help='Visualize distilled data')
        parser.add_argument('--mult_label_scaling', type=float, default = 1, help = "Multiplicative scaling for label initialisations")
        parser.add_argument('--add_label_scaling', type=float, default = 0, help = "Additive scaling for label initialisations")
        parser.add_argument('--add_first', type=bool, default=True, help="Perform add scaling before mult scaling for label inits?")
        parser.add_argument('--dist_metric', type=str, default='MSE', help="One of MSE | NRMSE | SSIM, only used with AIBD and CNDB")
        parser.add_argument('--invert_dist', type=bool, default=False, help="Should distance for label init be reversed? Only used with AIDB and CNDB")
        parser.add_argument('--freeze_data', type=bool, default=False, help="Should only labels and lr be learned (freeze data samples as random)?")



    def get_state(self):
        if hasattr(self, 'state'):
            return self.state

        self.opt, unknowns = self.parser.parse_known_args(namespace=State.UniqueNamespace())
        assert len(unknowns) == 0, 'Unexpected args: {}'.format(unknowns)
        self.state = State(self.opt)
        return self.set_state(self.state)

    def set_state(self, state, dummy=False):
        if state.opt.sample_n_nets is None:
            state.opt.sample_n_nets = state.opt.n_nets

        base_dir = state.get_base_directory()
        save_dir = state.get_save_directory()

        state.opt.start_time = time.strftime(r"%Y-%m-%d %H:%M:%S")

        # Usually only rank 0 can write to file (except logging, training many
        # nets, etc.) so let's set that flag before everything
        state.opt.distributed = state.world_size > 1
       
        state.world_rank = 0
        state.set_output_flag(not dummy)

        if not dummy:
            utils.mkdir(save_dir)

            

        _, state.opt.dataset_root, state.opt.nc, state.opt.input_size, state.opt.num_classes, \
            state.opt.dataset_normalization, state.opt.dataset_labels = datasets.get_info(state)
        if not state.opt.num_distill_classes:
            state.opt.num_distill_classes = state.opt.num_classes
        if not state.opt.init_labels:
            state.opt.init_labels = list(range(state.opt.num_distill_classes))
            
        # Write yaml
        yaml_str = yaml.dump(state.merge(public_only=True), default_flow_style=False, indent=4)

        

        # FROM HERE, we have saved options into yaml,
        #            can start assigning objects to opt, and
        #            modify the values for process-specific things
        def assert_divided_by_world_size(key, strict=True):
            val = getattr(state, key)
            if strict:
                assert val % state.world_size == 0, \
                    "expected {}={} to be divisible by the world size={}".format(key, val, state.world_size)
                val = val // state.world_size
            else:
                val = math.ceil(val / state.world_size)
            setattr(state, 'local_{}'.format(key), val)

        assert_divided_by_world_size('n_nets')



        if state.device_id < 0:
            state.opt.device = torch.device("cpu")
        else:
            torch.cuda.set_device(state.device_id)
            state.opt.device = torch.device("cuda:{}".format(state.device_id))

        if not dummy:
            if state.device.type == 'cuda' and torch.backends.cudnn.enabled:
                torch.backends.cudnn.benchmark = True

            seed = state.base_seed

            state.opt.seed = seed

            # torch.manual_seed will seed ALL GPUs.
            torch.random.default_generator.manual_seed(seed)
            torch.cuda.manual_seed(seed)
            np.random.seed(seed)
            random.seed(seed)

        
        # in case of downloading, to avoid race, let rank 0 download.
        train_dataset = datasets.get_dataset(state, 'train')
        test_dataset = datasets.get_dataset(state, 'test')




        return state


options = BaseOptions()
