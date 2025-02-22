import sys
import warnings
warnings.simplefilter("ignore", UserWarning)
sys.path.append('./release')

import argparse
import os
import pickle
import random
import shutil

import numpy as np
import seaborn as sns
import torch
import wandb
from data import GeneratorData
from joblib import Parallel, delayed
from matplotlib import pyplot as plt
from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import AllChem, rdmolfiles
from rdkit.Chem.Crippen import MolLogP
from rdkit.Chem.QED import qed
from rdkit.Chem.rdMolDescriptors import CalcTPSA
from reinforcement import Reinforcement
from stackRNN import StackAugmentedRNN
from torch import nn
from torch.nn import functional as F
from torch.optim.lr_scheduler import ExponentialLR, StepLR
from tqdm import tqdm, trange
from utils import canonical_smiles
from gensim.models import word2vec
from mol2vec.features import mol2alt_sentence, mol2sentence, MolSentence, DfVec, sentences2vec

import rewards as rwds
from Predictors.GINPredictor import Predictor as GINPredictor
from Predictors.RFRPredictor import RFRPredictor
from Predictors.SolvationPredictor import FreeSolvPredictor


RDLogger.DisableLog('rdApp.info')

from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import DotProduct, WhiteKernel
from sklearn.gaussian_process.kernels import RBF, ConstantKernel

parser = argparse.ArgumentParser()
parser.add_argument("--reward_function",
                    help="Reward Function linear/exponential/log/squared", default="linear")
parser.add_argument("--device", help="GPU/CPU", default="GPU")
parser.add_argument("--gen_data", default='./random.smi')
parser.add_argument("--num_iterations", default=100,
                    type=int, help="Number of iterations")
parser.add_argument("--use_wandb", default='yes',
                    help="Perform logging using wandb")
parser.add_argument("--use_checkpoint", default='yes',
                    help="Load from a checkpoint")
parser.add_argument("--adaptive_reward", default='yes',
                    help="Change reward with iterations")
parser.add_argument("--logP", default='no', help="Reward for LogP")
parser.add_argument("--qed", default='no', help="Reward for QED")
parser.add_argument("--tpsa", default='no', help="Reward for TPSA")
parser.add_argument("--solvation", default='no', help="Reward for Solvation")
parser.add_argument("--switch", default='no', help="switch reward function")
parser.add_argument("--predictor", default='dock',
                    help='Choose prediction algorithm')
parser.add_argument("--protein", default='4BTK', help='4BTK/6LU7')
parser.add_argument("--remarks", default="")
parser.add_argument("--logP_threshold", default=2.5, type=float)
parser.add_argument("--tpsa_threshold", default=100, type=float)
parser.add_argument("--solvation_threshold", default=-10, type=float)
parser.add_argument("--qed_threshold", default=0.8, type=float)
parser.add_argument("--switch_frequency", default=35, type=int)
parser.add_argument("--seed", default=0, type=int)

def update_std_threshold(gpr, X_test):
    '''
    Get updated gpr threshold
    '''
    y_pred = gpr.predict(X_test, return_std=True)
    counts, bins = np.histogram(y_pred[1], bins=6)
    print("Current std threshold: ", bins[2])
    return bins[2]
args = parser.parse_args()
with open("../Analysis/gpr_pretrained.pkl", 'rb') as f:
    gpr = pickle.load(f)
with open("../Analysis/molecules/labelled.pkl", 'rb') as f:
    df_cur_train = pickle.load(f)
with open("../Analysis/molecules/test.pkl", 'rb') as f:
    df_cur_test = pickle.load(f)
mol_model = word2vec.Word2Vec.load('../../mol2vec/examples/models/model_300dim.pkl')
hits = 0
miss = 0
uncertain_vec = []
uncertain_labels = []
X_cur, y_cur = df_cur_train['mol2vec'], df_cur_train['ba']
X_test, y_test = df_cur_test['mol2vec'], df_cur_test['ba']
std_threshold = update_std_threshold(gpr, X_test)
switch_frequency = args.switch_frequency
thresholds = {
    'TPSA': args.tpsa_threshold,
    'LogP': args.logP_threshold,
    'solvation': args.solvation_threshold,
    'QED': args.qed_threshold
}

receptor = args.protein
random.seed(args.seed)
if args.device == "CPU":
    device = torch.device('cpu')
    use_cuda = False
elif args.device == "GPU":
    if torch.cuda.is_available():
        use_cuda = True
        device = torch.device('cuda:0')
    else:
        print("Sorry! GPU not available Using CPU instead")
        device = torch.device('cpu')
        use_cuda = False
else:
    print("Invalid Device")
    quit(0)

OVERALL_INDEX = 0

if args.use_wandb == "yes":
    wandb.init(project=f"{args.reward_function}_{args.remarks}")
    wandb.config.update(args)

if args.reward_function == 'linear':
    get_reward = rwds.linear
if args.reward_function == 'exponential':
    get_reward = rwds.exponential
if args.reward_function == 'logarithmic':
    get_reward = rwds.logarithmic
if args.reward_function == 'squared':
    get_reward = rwds.squared

use_docking = True
use_qed = False
use_tpsa = False
use_solvation = False
use_logP = False
if args.logP == 'yes':
    use_logP = True
if args.qed == 'yes':
    use_qed = True
if args.tpsa == 'yes':
    use_tpsa = True
if args.solvation == 'yes':
    use_solvation = True

get_reward = rwds.MultiReward(
    get_reward, use_docking, use_logP, use_qed, use_tpsa, use_solvation, **thresholds)


if os.path.exists(f"./logs_{args.reward_function}_{args.remarks}") == False:
    os.mkdir(f"./logs_{args.reward_function}_{args.remarks}")
else:
    shutil.rmtree(f"./logs_{args.reward_function}_{args.remarks}")
    os.mkdir(f"./logs_{args.reward_function}_{args.remarks}")

if os.path.exists(f"./molecules_{args.reward_function}_{args.remarks}") == False:
    os.mkdir(f"./molecules_{args.reward_function}_{args.remarks}")
else:
    shutil.rmtree(f"./molecules_{args.reward_function}_{args.remarks}")
    os.mkdir(f"./molecules_{args.reward_function}_{args.remarks}")


if os.path.exists("./trajectories") == False:
    os.mkdir(f"./trajectories")
if os.path.exists("./rewards") == False:
    os.mkdir(f"./rewards")
if os.path.exists("./losses") == False:
    os.mkdir(f"./losses")
if os.path.exists("./models") == False:
    os.mkdir(f"./models")
if os.path.exists("./predictions") == False:
    os.mkdir("./predictions")


MODEL_NAME = f"./models/model_{args.reward_function}_{args.remarks}"
LOGS_DIR = f"./logs_{args.reward_function}_{args.remarks}"
MOL_DIR = f"./molecules_{args.reward_function}_{args.remarks}"

TRAJ_FILE = open(
    f"./trajectories/traj_{args.reward_function}_{args.remarks}", "w")
LOSS_FILE = f"./losses/{args.reward_function}_{args.remarks}"
REWARD_FILE = f"./rewards/{args.reward_function}_{args.remarks}"

gen_data_path = args.gen_data
tokens = ['<', '>', '#', '%', ')', '(', '+', '-', '/', '.', '1', '0', '3', '2', '5', '4', '7',
          '6', '9', '8', '=', 'A', '@', 'C', 'B', 'F', 'I', 'H', 'O', 'N', 'P', 'S', '[', ']',
          '\\', 'c', 'e', 'i', 'l', 'o', 'n', 'p', 's', 'r', '\n']

gen_data = GeneratorData(training_data_path=gen_data_path, delimiter='\t',
                         cols_to_read=[0], keep_header=True, tokens=tokens)


def dock_and_get_score(smile, test=False):
    global MOL_DIR
    global LOGS_DIR
    global OVERALL_INDEX
    mol_dir = MOL_DIR
    log_dir = LOGS_DIR
    try:
        path = "~/MGLTools-1.5.6/mgltools_x86_64Linux2_1.5.6/bin/python2.5 ~/MGLTools-1.5.6/mgltools_x86_64Linux2_1.5.6/MGLToolsPckgs/AutoDockTools/Utilities24"
        #  path = "~/MGLTools-1.5.6/MGLToolsPckgs/AutoDockTools/Utilities24"
        mol = Chem.MolFromSmiles(smile)
        AllChem.EmbedMolecule(mol)
        if test == True:
            MOL_DIR = f"validation_mols_{args.reward_function}_{args.remarks}"
            LOGS_DIR = f"validation_log_{args.reward_function}_{args.remarks}"

            if os.path.exists(MOL_DIR):
                shutil.rmtree(MOL_DIR)
                os.mkdir(MOL_DIR)
            else:
                os.mkdir(MOL_DIR)

            if os.path.exists(LOGS_DIR):
                shutil.rmtree(LOGS_DIR)
                os.mkdir(LOGS_DIR)
            else:
                os.mkdir(LOGS_DIR)
            #print(MOL_DIR, LOGS_DIR)
        rdmolfiles.MolToPDBFile(mol, f"{MOL_DIR}/{str(OVERALL_INDEX)}.pdb")

        os.system(
            f"{path}/prepare_ligand4.py -l {MOL_DIR}/{str(OVERALL_INDEX)}.pdb -o {MOL_DIR}/{str(OVERALL_INDEX)}.pdbqt  > /dev/null 2>&1")
        os.system(
            f"{path}/prepare_receptor4.py -r {receptor}.pdb  > /dev/null 2>&1")
        os.system(
            f"{path}/prepare_gpf4.py -i {receptor}_ref.gpf -l {MOL_DIR}/{str(OVERALL_INDEX)}.pdbqt -r {receptor}.pdbqt > /dev/null 2>&1")

        os.system(f"autogrid4 -p {receptor}.gpf > /dev/null 2>&1")
        os.system(
            f"~/AutoDock-GPU/bin/autodock_gpu_128wi -ffile {receptor}.maps.fld -lfile {MOL_DIR}/{str(OVERALL_INDEX)}.pdbqt -resnam {LOGS_DIR}/{str(OVERALL_INDEX)} -nrun 10 -devnum 1 > /dev/null 2>&1")

        cmd = f"cat {LOGS_DIR}/{str(OVERALL_INDEX)}.dlg | grep -i ranking | tr -s '\t' ' ' | cut -d ' ' -f 5 | head -n1"
        stream = os.popen(cmd)
        output = float(stream.read().strip())
        # print(LOGS_DIR, OVERALL_INDEX)
        # print(output, smile)
        OVERALL_INDEX += 1
        MOL_DIR = mol_dir
        LOGS_DIR = log_dir
        return output
    except Exception as e:
        MOL_DIR = mol_dir
        LOGS_DIR = log_dir
        # print(smile)
        OVERALL_INDEX += 1
        print(f"Did Not Complete because of {e}")
        return 0


def smiles_to_mol2vec(smiles):
    global mol_model
    a = [Chem.MolFromSmiles(smile) for smile in smiles]
    b = [MolSentence(mol2alt_sentence(mol, 1)) for mol in a]
    c = sentences2vec(b, mol_model, unseen='UNK')
    return c


class Predictor(object):
    def __init__(self, path):
        super(Predictor, self).__init__()
        self.path = path

    def predict(self, smiles, test=False, use_tqdm=False):
        global gpr
        global hits
        global miss
        global uncertain_vec
        global uncertain_labels
        global std_threshold
        canonical_indices = []
        invalid_indices = []
        if use_tqdm:
            pbar = tqdm(range(len(smiles)))
        else:
            pbar = range(len(smiles))
        for i in pbar:
            sm = smiles[i]
            if use_tqdm:
                pbar.set_description("Calculating predictions...")
            try:
                sm = Chem.MolToSmiles(Chem.MolFromSmiles(sm))
                if len(sm) == 0:
                    invalid_indices.append(i)
                else:
                    canonical_indices.append(i)
            except:
                invalid_indices.append(i)
        canonical_smiles = [smiles[i] for i in canonical_indices]
        invalid_smiles = [smiles[i] for i in invalid_indices]
        if len(canonical_indices) == 0:
            return canonical_smiles, [], invalid_smiles
        mol2vec_cur = smiles_to_mol2vec(canonical_smiles)
        preds = gpr.predict(mol2vec_cur, return_std=True)
        prediction = []
        for i in range(0, len(preds[0])):
            if preds[1][i] > std_threshold:
                score = dock_and_get_score(canonical_smiles[i], test)
                prediction.append(score)
                if score >= 0:
                    continue
                miss = miss + 1
                #print(miss, preds[1][i])
                if miss % 20 == 0:
                    print("hits : miss ratio - ", hits, " : ", miss)
                uncertain_vec.append(mol2vec_cur[i])
                uncertain_labels.append(score)
                if len(uncertain_labels) == 750:
                    X_cur.extend(
                        uncertain_vec[:int(0.9*len(uncertain_labels))])
                    y_cur.extend(
                        uncertain_labels[:int(0.9*len(uncertain_labels))])
                    X_test.extend(
                        uncertain_vec[int(0.1*len(uncertain_labels)):])
                    y_test.extend(
                        uncertain_labels[int(0.1*len(uncertain_labels)):]
                    )
                    kernel = RBF(2.0) + WhiteKernel(1.0)
                    gpr = GaussianProcessRegressor(
                        kernel=kernel, random_state=0, alpha=0.1).fit(X_cur, y_cur)
                    y_pred = gpr.predict(X_test)
                    print('MAE: ', mean_absolute_error(y_pred, y_test))
                    print('MSE: ', mean_squared_error(y_pred, y_test))
                    print('R2: ', r2_score(y_pred, y_test))
                    print("-------Below -9 kcal/mol--------")
                    y_new = [y_pred[i]
                             for i in range(0, len(y_test)) if y_test[i] < -9]
                    y_test_new = [y_test[i]
                                  for i in range(0, len(y_test)) if y_test[i] < -9]
                    print('MAE: ', mean_absolute_error(y_test_new, y_new))
                    print('MSE: ', mean_squared_error(y_test_new, y_new))
                    print('R2: ', r2_score(y_test_new, y_new))
                    std_threshold = update_std_threshold(gpr, X_test)
                    uncertain_labels = []
                    uncertain_vec = []
                    df_cur_train = {"mol2vec": X_cur, "ba": y_cur}
                    df_cur_test = {"mol2vec": X_test, "ba": y_test}
                    with open("../Analysis/molecules/labelled.pkl", "wb") as f:
                        pickle.dump(df_cur_train, f)
                    with open("../Analysis/molecules/test.pkl", "wb") as f:
                        pickle.dump(df_cur_test, f)
                    with open("../Analysis/gpr_pretrained.pkl", "wb") as f:
                        pickle.dump(gpr, f)
            else:
                prediction.append(preds[0][i])
                hits = hits + 1
        return canonical_smiles, prediction, invalid_smiles


def estimate_and_update(generator, predictor, n_to_generate):
    generated = []
    pbar = tqdm(range(n_to_generate))
    for i in pbar:
        pbar.set_description("Generating molecules...")
        generated.append(generator.evaluate(gen_data, predict_len=120)[1:-1])

    sanitized = canonical_smiles(
        generated, sanitize=False, throw_warning=False)[:-1]
    unique_smiles = list(np.unique(sanitized))[1:]
    smiles, prediction, nan_smiles = predictor.predict(
        unique_smiles, test=True, use_tqdm=True)

    return smiles, prediction


def simple_moving_average(previous_values, new_value, ma_window_size=10):
    value_ma = np.sum(previous_values[-(ma_window_size-1):]) + new_value
    value_ma = value_ma/(len(previous_values[-(ma_window_size-1):]) + 1)
    return value_ma


if args.predictor == 'dock':
    my_predictor = Predictor("")

if args.predictor != 'dock':
    if args.protein == '6LU7':
        print("Predictor models not supported for 6LU7")
        quit(0)
    elif args.predictor == 'rfr':
        my_predictor = RFRPredictor('./Predictors/RFRPredictor.pkl')
    elif args.predictor == 'gin':
        my_predictor = GINPredictor('./Predictors/GINPredictor.tar')

if args.use_checkpoint == "yes":
    if os.path.exists(f"./models/model_{args.reward_function}_{args.remarks}") == True:
        model_path = f"./models/model_{args.reward_function}_{args.remarks}"
    else:
        model_path = './checkpoints/generator/checkpoint_biggest_rnn'
else:
    model_path = './checkpoints/generator/checkpoint_biggest_rnn'


hidden_size = 1500
stack_width = 1500
stack_depth = 200
layer_type = 'GRU'
lr = 0.001
optimizer_instance = torch.optim.Adadelta
n_to_generate = 100
n_policy_replay = 10
n_policy = 15
n_iterations = args.num_iterations

generator = StackAugmentedRNN(input_size=gen_data.n_characters,
                              hidden_size=hidden_size,
                              output_size=gen_data.n_characters,
                              layer_type=layer_type,
                              n_layers=1, is_bidirectional=False, has_stack=True,
                              stack_width=stack_width, stack_depth=stack_depth,
                              use_cuda=use_cuda,
                              optimizer_instance=optimizer_instance, lr=lr)
generator.load_model(model_path)

RL = Reinforcement(generator, my_predictor, get_reward)

rewards = []
rl_losses = []
preds = []
logp_iter = []
solvation_iter = []
qed_iter = []
tpsa_iter = []
solvation_predictor = FreeSolvPredictor('./Predictors/SolvationPredictor.tar')
PRED_FILE = f"./predictions/{args.reward_function}_{args.remarks}"

use_docking = False
use_logP = True
use_qed = False

use_arr = np.array([False, False, True])
for i in range(n_iterations):
    if args.switch == 'yes':
        if args.logP == 'yes' and args.qed == 'yes':
            if i % switch_frequency == 0:
                use_arr = np.roll(use_arr, 1)
                use_docking, use_logP, use_qed = use_arr
            get_reward = rwds.MultiReward(
                rwds.exponential, use_docking, use_logP, use_qed, use_tpsa, use_solvation, **thresholds)
            print(get_reward)
        if args.logP == 'yes' and args.qed == 'no':
            if i % switch_frequency == 0:
                use_logP = not use_logP
                use_docking = not use_docking
            get_reward = rwds.MultiReward(
                rwds.exponential, use_docking, use_logP, use_qed, use_tpsa, use_solvation, **thresholds)
            print(get_reward)
    for j in trange(n_policy, desc="Policy Gradient...."):
        if args.adaptive_reward == 'yes':
            cur_reward, cur_loss = RL.policy_gradient(
                gen_data,  get_reward, OVERALL_INDEX)
        else:
            cur_reward, cur_loss = RL.policy_gradient(gen_data, get_reward)
        rewards.append(simple_moving_average(rewards, cur_reward))
        rl_losses.append(simple_moving_average(rl_losses, cur_loss))

    smiles_cur, prediction_cur = estimate_and_update(
        RL.generator, my_predictor, n_to_generate)
    preds.append(sum(prediction_cur)/len(prediction_cur))
    logps = [MolLogP(Chem.MolFromSmiles(sm)) for sm in smiles_cur]
    tpsas = [CalcTPSA(Chem.MolFromSmiles(sm)) for sm in smiles_cur]
    qeds = []
    for sm in smiles_cur:
        try:
            qeds.append(qed(Chem.MolFromSmiles(sm)))
        except:
            pass
    _, solvations, _ = solvation_predictor.predict(smiles_cur)

    logp_iter.append(np.mean(logps))
    solvation_iter.append(np.mean(solvations))
    qed_iter.append(np.mean(qeds))
    tpsa_iter.append(np.mean(tpsas))
    print("Iter: ", i)
    print(f"BA: {preds[-1]}")
    print(f"LogP {logp_iter[-1]}")
    print(f"Hydration {solvation_iter[-1]}")
    print(f"TPSA {tpsa_iter[-1]}")
    print(f"QED {qed_iter[-1]}")
    RL.generator.save_model(f"{MODEL_NAME}")

    if args.use_wandb == 'yes':
        wandb.log({
            "loss": rewards[-1],
            "reward": rl_losses[-1],
            "predictions": preds[-1],
            "logP": sum(logps) / len(logps),
            "TPSA": sum(tpsas) / len(tpsas),
            "QED": sum(qeds) / len(qeds),
            "Solvation": sum(solvations) / len(solvations)
        })
        wandb.save(MODEL_NAME)
    np.savetxt(LOSS_FILE, rl_losses)
    np.savetxt(REWARD_FILE, rewards)
    np.savetxt(PRED_FILE, preds)

TRAJ_FILE.close()
if args.use_wandb == 'yes':
    wandb.save(MODEL_NAME)
