# Implementation of:
import numpy as np
import argparse
import os.path as osp
from dataset import get_dataset_split, get_ogb_split
from dataset_cpf import get_dataset_benchmark
import torch
import torch.nn.functional as F
from models import GCN, SAGE, GAT, APPNPM, SGC
from mlp import MLP
from sanity_check_mlpmix_ogb_p3 import get_sampler, setup_seed

parser = argparse.ArgumentParser()
parser.add_argument('--lamb', type=float, default=0.0,
                    help='Balances loss from hard labels and teacher outputs')
parser.add_argument('--hidden', type=int, default=256, help='hidden dimension.')
parser.add_argument('--model', type=str, default='SAGE', help='GCN, SAGE, GAT, APPNP, SGC')
parser.add_argument('--dataset', type=str, default='ogbn-arxiv', help='Cora, CiteSeer, PubMed')
parser.add_argument('--num_layer', type=int, default=3, help='hidden dimension.')
parser.add_argument('--epochs', type=int, default=500, help='hidden dimension.')
parser.add_argument('--patience', type=int, default=50, help='hidden dimension.')
parser.add_argument('--use_norm', type=int, default=0, help='hidden dimension.')
parser.add_argument('--runs', type=int, default=1, help='hidden dimension.')
parser.add_argument('--device', type=int, default=2, help='hidden dimension.')
parser.add_argument('--dropout', type=float, default=0.5, help='hidden dimension.')
parser.add_argument('--golden', type=int, default=0, help='hidden dimension.')
parser.add_argument('--lr', type=float, default=0.01, help='hidden dimension.')
parser.add_argument('--tau', type=float, default=0.07, help='hidden dimension.')
parser.add_argument('--weight_decay', type=float, default=0.0, help='hidden dimension.')
parser.add_argument('--mix_ratio', type=float, default=0.5, help='hidden dimension.')
parser.add_argument('--seed', type=int, default=0, help='2.')
parser.add_argument('--use_val', type=int, default=1, help='2.')

# para for data split
parser.add_argument('--train_examples_per_class', type=int, default=20, help='hidden dimension.')
parser.add_argument('--val_examples_per_class', type=int, default=30, help='hidden dimension.')
parser.add_argument('--pos_sample', type=float, default=0.6, help='hidden dimension.')
parser.add_argument('--neg_sample', type=float, default=0.1, help='hidden dimension.')
parser.add_argument('--generate', type=int, default=0, help='1|0 perform mixup each episode.')
parser.add_argument('--add_soft', type=int, default=1, help='hidden dimension.')
parser.add_argument('--num_class', type=int, default=0, help='hidden dimension.')
parser.add_argument('--policy_layer', type=int, default=2, help='hidden dimension.')
parser.add_argument('--policy_hidden', type=int, default=128, help='hidden dimension.')
# parameters for mlp student
parser.add_argument('--lr_mlp', type=float, default=0.01, help='[0.01, 0.005, 0.001].')
parser.add_argument('--dropout_mlp', type=float, default=0.0, help='[0.1, 0.2, 0.3, 0.4, 0.5, 0.6]')
parser.add_argument('--num_layer_mlp', type=int, default=3, help='hidden dimension.')
parser.add_argument('--hidden_mlp', type=int, default=256, help='hidden dimension.')
parser.add_argument('--weight_decay_mlp', type=float, default=0.0, help='[0.0, 0.001, 0.002, 0.005, 0.01]')
# RL
parser.add_argument(
    '--sampler',
    type=str,
    default='single_policy_gradient',
    choices=[
        "original",
        "random",
        "single_policy_gradient",
        "single_actor_critic",  # This is problomatic
    ],
)
parser.add_argument(
    '--rl_start_epoch',
    type=int,
    default=20,
)


class get_model(torch.nn.Module):
    def __init__(self, in_feat, num_classes, args):
        super().__init__()
        if args.model == 'GCN':
            self.conv = GCN(in_feat, args.hidden, num_classes, args.num_layer, norm=args.use_norm)
        elif args.model == 'SAGE':
            self.conv = SAGE(in_feat, args.hidden, num_classes, args.num_layer, norm=args.use_norm)
        elif args.model == 'GAT':
            self.conv = GAT(in_feat, args.hidden, num_classes, args.num_layer, heads=8)
        elif args.model == 'APPNP':
            self.conv = APPNPM(in_feat, args.hidden, num_classes, args.num_layer, K=10, alpha=0.2)
        elif args.model == 'SGC':
            self.conv = SGC(in_feat, args.hidden, num_classes, args.num_layer, K=2)

    def forward(self, data):
        x = self.conv(data)
        return x


def train_teacher(gnn, gnn_optimizer, data):
    gnn.train()
    gnn_optimizer.zero_grad()
    out = gnn(data)
    loss = F.cross_entropy(out[data.train_mask], data.y[data.train_mask])
    loss.backward()
    gnn_optimizer.step()
    return float(loss)


def print_configuration(args):
    print('--> Experiment configuration')
    for key, value in vars(args).items():
        print('{}: {}'.format(key, value))


def train_teacher_active(gnn, gnn_optimizer, data, y_soft, sampler):
    gnn.train()
    gnn_optimizer.zero_grad()
    out = gnn(data)
    action = sampler.sample(out)
    loss = F.kl_div(out.log_softmax(dim=-1), y_soft, reduction='none', log_target=False).sum(dim=-1, keepdim=True)
    loss = loss * action
    loss = torch.sum(loss) / torch.sum(action)
    loss.backward()
    gnn_optimizer.step()
    return float(loss)


@torch.no_grad()
def test_teacher(gnn, data):
    gnn.eval()
    pred = gnn(data).argmax(dim=-1)
    accs = []
    for _, mask in data('train_mask', 'val_mask', 'test_mask'):
        accs.append(int((pred[mask] == data.y[mask]).sum()) / int(mask.sum()))
    return accs


def pretrain_teacher(data, in_feat, num_classes, save_path_teacher, device, args):
    print('Training Teacher GNN:')
    gnn = get_model(in_feat, num_classes, args).to(device)
    gnn_optimizer = torch.optim.Adam(gnn.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    best_val = best_test = cnt = 0
    for epoch in range(1, args.epochs + 1):
        loss = train_teacher(gnn, gnn_optimizer, data)
        train_acc, val_acc, test_acc = test_teacher(gnn, data)
        if val_acc > best_val:
            best_val = val_acc
            best_test = test_acc
            cnt = 0
            torch.save(gnn.state_dict(), save_path_teacher)
        else:
            cnt += 1
        print(f'Epoch: {epoch:03d}, Loss: {loss:.4f}, Train: {train_acc:.4f}, '
              f'Val: {val_acc:.4f}, Test: {test_acc:.4f}')
        if cnt == args.patience:
            print(f'Early stop at epoch: {epoch:03d}')
            break
    gnn.load_state_dict(torch.load(save_path_teacher))
    train_acc, val_acc, test_acc = test_teacher(gnn, data)
    print(f'### Teacher model: Best val_acc: {val_acc:.4f} Best test_acc: {test_acc:.4f}')
    with torch.no_grad():  # Obtain soft labels from the GNN:
        # y_soft = gnn(data).log_softmax(dim=-1)
        y_soft = torch.softmax(gnn(data) / args.tau, dim=-1)
    return y_soft, val_acc, test_acc


def pretrain_teacher_active(data, in_feat, num_classes, y_label, save_path_teacher, sampler, device, args):
    print('Training Teacher GNN:')
    gnn = get_model(in_feat, num_classes, args).to(device)
    gnn_optimizer = torch.optim.Adam(gnn.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    best_val = best_test = cnt = 0
    for epoch in range(1, args.epochs + 1):
        loss = train_teacher_active(gnn, gnn_optimizer, data, y_label, sampler)
        train_acc, val_acc, test_acc = test_teacher(gnn, data)
        if val_acc > best_val:
            best_val = val_acc
            best_test = test_acc
            cnt = 0
            torch.save(gnn.state_dict(), save_path_teacher)
        else:
            cnt += 1
        print(f'Epoch: {epoch:03d}, Loss: {loss:.4f}, Train: {train_acc:.4f}, '
              f'Val: {val_acc:.4f}, Test: {test_acc:.4f}')
        if cnt == args.patience:
            print(f'Early stop at epoch: {epoch:03d}')
            break
    gnn.load_state_dict(torch.load(save_path_teacher))
    train_acc, val_acc, test_acc = test_teacher(gnn, data)
    print(f'### Teacher model: Best val_acc: {val_acc:.4f} Best test_acc: {test_acc:.4f}')
    with torch.no_grad():  # Obtain soft labels from the GNN:
        y_soft = gnn(data).log_softmax(dim=-1)
    return val_acc, test_acc


def estimate_statis(action, y_mask):
    # y_mask is [1,0,1] 1 means the prediction is correct.
    action = action.view(-1)
    pred_indicator = (action == y_mask.int()).int()
    pred_ones = torch.nonzero(action).view(-1)
    one_act = pred_indicator[pred_ones]
    TP = one_act.sum()
    FP = one_act.shape[0] - one_act.sum()

    zeros_index = torch.nonzero((action - 1).int()).view(-1)
    zero_act = pred_indicator[zeros_index]
    TN = zero_act.sum().item()
    FN = zero_act.shape[0] - zero_act.sum().item()
    print(f'TP: {TP} FP: {FP} TN: {TN} FN: {FN}')
    print(f'TP: {TP/(TP+FP):.4f} FN: {FP/(TP+FP):.4f} TN: {TN/(TN+FN + 0.5):.4f} FN: {FN/(TN+FN + 0.5):.4f}')


def train_student(mlp, mlp_optimizer, data, y_soft, y_mask, train_mask_true, sampler, args):
    mlp.train()
    mlp_optimizer.zero_grad()
    out = mlp(data.x)
    # x_hidden = mlp.encode(data.x[data.u_mask])
    # x_hidden = mlp.encode(data.x)

    x_hidden = mlp.encode(data.x[data.u_mask])
    if args.add_soft:
        x_hidden = torch.cat([x_hidden, y_soft[data.u_mask]], dim=1)
    action = sampler.sample(x_hidden, training=False)
    # data_stu = data.clone()
    # if args.add_soft:
    #     x_hidden = torch.cat((x_hidden, y_soft), dim=1)
    # data_stu.x = x_hidden
    # # x_hidden = torch.cat([x_hidden, y_soft[data.u_mask]], dim=1)
    # action = sampler.sample(data_stu, training=False)[data.u_mask]
    correct_ratio = (action.view(-1) == y_mask[data.u_mask]).to(torch.float)
    correct_ratio = correct_ratio.sum()/correct_ratio.shape[0]
    estimate_statis(action, y_mask[data.u_mask])
    # loss for labeled data
    loss1 = F.kl_div(out.log_softmax(dim=-1)[train_mask_true], y_soft[train_mask_true], log_target=False, reduction='none').sum(dim=-1, keepdim=True)
    loss = F.kl_div(out.log_softmax(dim=-1)[data.u_mask], y_soft[data.u_mask], reduction='none', log_target=False).sum(dim=-1, keepdim=True)
    loss = loss * action.view(-1, 1)
    loss = (torch.sum(loss1) + 2 * torch.sum(loss)) / (train_mask_true.sum() + torch.sum(action))
    # loss = torch.sum(loss) / torch.sum(action)
    loss.backward()
    mlp_optimizer.step()
    return float(loss), float(correct_ratio), action


def train_student_god(mlp, mlp_optimizer, data, y_soft, y_mask, args):
    mlp.train()
    mlp_optimizer.zero_grad()
    out = mlp(data.x)
    loss1 = F.cross_entropy(out[data.train_mask], data.y[data.train_mask])
    # loss2 = F.kl_div(out.log_softmax(dim=-1)[y_mask], y_soft[y_mask], reduction='batchmean', log_target=True)
    loss2 = F.kl_div(out.log_softmax(dim=-1), y_soft, reduction='batchmean', log_target=False)
    loss = args.lamb * loss1 + (1 - args.lamb) * loss2
    loss.backward()
    mlp_optimizer.step()
    return float(loss), 1, None


def train_student_god2(mlp, mlp_optimizer, data, y_soft, y_mask, args):
    mlp.train()
    mlp_optimizer.zero_grad()
    out = mlp(data.x)
    loss1 = F.cross_entropy(out[data.train_mask], data.y[data.train_mask])
    loss2 = F.kl_div(out.log_softmax(dim=-1)[y_mask], y_soft[y_mask], reduction='batchmean', log_target=False)
    # loss2 = F.kl_div(out.log_softmax(dim=-1), y_soft, reduction='batchmean', log_target=False)
    loss = args.lamb * loss1 + (1 - args.lamb) * loss2
    loss.backward()
    mlp_optimizer.step()
    return float(loss)

def test_student(mlp, data):
    mlp.eval()
    pred = mlp(data.x).argmax(dim=-1)
    accs = []
    for _, mask in data('train_mask', 'val_mask', 'test_mask'):
        accs.append(int((pred[mask] == data.y[mask]).sum()) / int(mask.sum()))
    return accs


def student_train(data, in_feat, hidden, num_classes, y_soft, y_mask, train_mask_true, lr, weight_decay, dropout, save_path_student, sampler, args, device):
    channel_list = [args.hidden_mlp] * (args.num_layer - 1)
    channel_list = [in_feat] + channel_list + [num_classes]
    mlp = MLP(channel_list, dropout=args.dropout_mlp, batch_norm=True).to(device)
    mlp_optimizer = torch.optim.Adam(mlp.parameters(), lr=args.lr_mlp, weight_decay=args.weight_decay_mlp)
    print('Training Student MLP:')
    random_sampler = get_sampler('random')(device, args)
    # test_mask = data.val_mask.clone()
    # test_mask[data.train_mask] = True

    pretrain = 100
    best_val = best_epoch = cnt = 0
    best_loss = 100
    best_action = None
    for epoch in range(1, args.epochs * 2 + 1):
        if epoch > pretrain:
            if args.use_val:
                sampler.learn(mlp, data, y_soft, data.val_mask)
            else:
                sampler.learn(mlp, data, y_soft, data.train_mask)
            cut_sampler = sampler if epoch > pretrain else random_sampler
            loss, correct_ratio, action = train_student(mlp, mlp_optimizer, data, y_soft, y_mask, train_mask_true, cut_sampler, args)
        else:
            loss, correct_ratio, action = train_student_god(mlp, mlp_optimizer, data, y_soft, y_mask, args)
        train_acc, val_acc, test_acc = test_student(mlp, data)

        if val_acc > best_val:
            best_val = val_acc
            best_loss = loss
            best_epoch = epoch
            cnt = 0
            torch.save(mlp.state_dict(), save_path_student)
            best_action = action
        else:
            cnt += 1
        print(f'Epoch: {epoch:03d}, Loss: {loss:.4f}, Corr_ratio: {correct_ratio:.4f} Train: {train_acc:.4f}, '
              f'Val: {val_acc:.4f}, Test: {test_acc:.4f}')
        if cnt == 200:
            print(f'Student Early stop at epoch: {epoch:03d}')
            break
    print('### Best action statistic with best epoch: {}'.format(best_epoch))
    estimate_statis(best_action, y_mask[data.u_mask])
    mlp.load_state_dict(torch.load(save_path_student))
    train_acc, val_acc, test_acc = test_student(mlp, data)
    print(f'### Student model: Best val_acc: {val_acc:.4f} Best test_acc: {test_acc:.4f}')
    return val_acc, test_acc, best_action


def student_train_god(data, in_feat, hidden, num_classes, y_soft, y_mask, lr, weight_decay, dropout, save_path_student, args, device):
    channel_list = [args.hidden_mlp] * (args.num_layer - 1)
    channel_list = [in_feat] + channel_list + [num_classes]
    mlp = MLP(channel_list, dropout=args.dropout_mlp, batch_norm=True).to(device)
    mlp_optimizer = torch.optim.Adam(mlp.parameters(), lr=args.lr_mlp, weight_decay=args.weight_decay_mlp)
    print('Training Student MLP with God Guidence:')
    best_val = best_test = cnt = 0
    for epoch in range(1, args.epochs * 2 + 1):
        loss = train_student_god2(mlp, mlp_optimizer, data, y_soft, y_mask, args)
        train_acc, val_acc, test_acc = test_student(mlp, data)

        if val_acc > best_val:
            best_val = val_acc
            best_test = test_acc
            cnt = 0
            torch.save(mlp.state_dict(), save_path_student)
        else:
            cnt += 1
        print(f'Epoch: {epoch:03d}, Loss: {loss:.4f}, Train: {train_acc:.4f}, '
              f'Val: {val_acc:.4f}, Test: {test_acc:.4f}')
        if cnt == 200:
            print(f'Student Early stop at epoch: {epoch:03d}')
            break
    mlp.load_state_dict(torch.load(save_path_student))
    train_acc, val_acc, test_acc = test_student(mlp, data)
    print(f'### Student model: Best val_acc: {val_acc:.4f} Best test_acc: {test_acc:.4f}')
    return val_acc, test_acc


def soft_label_statistic(y_soft, y_soft_label, y_mask):
    index_pros = torch.nonzero(y_mask).view(-1)
    index_cons = torch.nonzero(y_mask - 1).view(-1)

    pred_pros_score = torch.stack([y_soft[i, y_soft_label[i]] for i in index_pros])
    pred_cons_score = torch.stack([y_soft[i, y_soft_label[i]] for i in index_cons])
    pros_mean = torch.mean(pred_pros_score)
    pros_std = torch.std(pred_pros_score)
    cons_mean = torch.mean(pred_cons_score)
    cons_std = torch.std(pred_cons_score)

    print('Positive: mean: {} std: {}'.format(pros_mean, pros_std))
    print('Negative: mean: {} std: {}'.format(cons_mean, cons_std))


def gnn_understand(y_soft, data, num_class):
    train_index = torch.where(data.train_mask == True)[0]
    valid_index = torch.where(data.val_mask == True)[0]
    test_index = torch.where(data.test_mask == True)[0]
    y_pred = torch.argmax(y_soft, dim=1) == data.y
    train_label = data.y[train_index]
    valid_label = data.y[valid_index]
    test_label = data.y[test_index]
    for i in range(num_class):
        train_i = train_label == i
        train_index_i = train_index[train_i]
        if train_index_i.shape[0] > 0:
            train_i_pred = y_pred[train_index_i]
            train_TP = train_i_pred.sum()
            train_FP = train_i_pred.shape[0] - train_TP
        else:
            train_TP = 0
            train_FP = 0

        val_i = valid_label == i
        val_index_i = valid_index[val_i]
        if val_index_i.shape[0] > 0:
            val_i_pred = y_pred[val_index_i]
            val_TP = val_i_pred.sum()
            val_FP = val_i_pred.shape[0] - val_TP
        else:
            val_TP = 0
            val_FP = 0

        test_i = test_label == i
        test_index_i = test_index[test_i]
        if test_index_i.shape[0] > 0:
            test_i_pred = y_pred[test_index_i]
            test_TP = test_i_pred.sum()
            test_FP = test_i_pred.shape[0] - test_TP
        else:
            test_TP = 0
            test_FP = 0
        print('Class: {} train_TP: {} train_FP: {} ; val_TP: {} val_FP: {} ; test_TP: {} test_FP: {} '.format(i, train_TP, train_FP, val_TP, val_FP, test_TP, test_FP))


def train_test(data, in_feat, num_classes, args, save_path_teacher, save_path_student, sampler, device):
    y_soft, teacher_val, teacher_test = pretrain_teacher(data, in_feat, num_classes, save_path_teacher, device, args)

    gnn_understand(y_soft, data, num_classes)

    y_soft_label = torch.argmax(y_soft, dim=-1)
    train_fail = y_soft_label[data.train_mask] != data.y[data.train_mask]
    train_index = torch.nonzero(data.train_mask.int()).view(-1)
    train_index_true = train_index[train_fail]
    # if train_index_true.shape[0] > 0:
    #     ones = torch.eye(num_classes).to(device)
    #     train_labels = ones[data.y[train_index_true]]
    #     y_soft[train_index_true] = train_labels
    print('### correct_train: {} wrong_train: {}'.format(data.train_mask.sum(), train_index_true.shape[0]))
    y_mask = (y_soft_label == data.y).to(torch.int64)
    soft_label_statistic(y_soft, y_soft_label, y_mask)

    train_mask = y_soft_label[data.train_mask] == data.y[data.train_mask]
    train_index = torch.nonzero(data.train_mask.int()).view(-1)
    train_index_true = train_index[train_mask]
    train_mask_true = torch.zeros_like(data.train_mask).to(torch.bool)
    train_mask_true[train_index_true] = True

    student_val, student_test, best_action = student_train(data, in_feat, args.hidden, num_classes, y_soft, y_mask, train_mask_true, args.lr,
                                              args.weight_decay, args.dropout, save_path_student, sampler, args, device)
    index = torch.nonzero(best_action).view(-1)     # u_mask
    y_mask_index = torch.nonzero(data.u_mask.int()).view(-1)
    y_pred = y_mask_index[index]
    # y_mask2 = data.train_mask.clone()
    y_mask2 = train_mask_true.clone()
    # y_mask2[train_index_true] = False
    y_mask2[y_pred] = True
    student_val_god, student_test_god = student_train_god(data, in_feat, args.hidden, num_classes, y_soft, y_mask2,
                                                           args.lr,
                                                           args.weight_decay, args.dropout, save_path_student,
                                                           args, device)

    return [teacher_val, teacher_test], [student_val, student_test], [student_val_god, student_test_god], y_mask


def main(data, in_feat, num_classes, args, save_path_teacher, save_path_student, device):
    results = {
        'teacher': [],
        'student_god': [],
        'student_soft': [],
               }

    for run in range(args.runs):
        sampler = get_sampler(args.sampler)(device, args)
        print('--- Start run: ',  run + 1)
        teacher_result, \
        student_result, student_result_god, y_mask = train_test(data, in_feat, num_classes, args, save_path_teacher, save_path_student, sampler, device)
        results['teacher'].append(teacher_result)
        # results['teacher_soft'].append(teacher_result_soft)
        results['student_soft'].append(student_result)
        results['student_god'].append(student_result_god)

        print('### Test results for {}/{}'.format(run, args.runs))
        print(f'### Final Results for the Teacher model are acc_val: {teacher_result[0]:.4f} acc_test: {teacher_result[1]:.4f}')
        print(f'--- We have {int(y_mask.sum()):03d}/{y_mask.shape[0]:03d} god examples and best test result: {float(y_mask.sum() / y_mask.shape[0])}')

        # print(f'### Active Results for the Teacher are acc_val: {teacher_result_soft[0]:.4f} acc_test: {teacher_result_soft[1]:.4f}')
        # print(f'### Active Results for the Teacher god are acc_val: {teacher_result_soft_god[0]:.4f} acc_test: {teacher_result_soft_god[1]:.4f}')

        print(f'### Final Results for the Student model are acc_val: {student_result[0]:.4f} acc_test: {student_result[1]:.4f}')
        print(f'### Final Results for the Student god are acc_val: {student_result_god[0]:.4f} acc_test: {student_result_god[1]:.4f}')

    for key in results.keys():
        result_ = np.array(results[key])
        print('result shape {}'.format(result_.shape))
        assert result_.shape[1] == 2
        acc_val, acc_test = np.mean(result_, axis=0)[0], np.mean(result_, axis=0)[1]
        std_val, std_test = np.std(result_, axis=0)[0], np.std(result_, axis=0)[1]
        print('Final result {} acc_val: {} acc_val_std: {} acc_test: {} acc_test_std: {}'.format(key, acc_val, std_val, acc_test, std_test))


if __name__ == "__main__":
    args = parser.parse_args()
    path = osp.join(osp.dirname(osp.realpath(__file__)), 'datasets')
    setup_seed(args.seed)
    if args.dataset.startswith('ogbn'):
        data = get_ogb_split(path, args.dataset)
    elif args.dataset in ['amazon_electronics_computers', 'amazon_electronics_photo']:
        print('--- Loading data according to CPF')
        data = get_dataset_benchmark(path, args.dataset, args.train_examples_per_class, args.val_examples_per_class)
    else:

        data = get_dataset_split(path, args.dataset, args.train_examples_per_class, args.val_examples_per_class)

    device = f'cuda:{args.device}' if torch.cuda.is_available() else 'cpu'
    data = data.to(device)
    in_feat = data.num_node_features
    args.hidden_input = in_feat
    num_classes = int(torch.max(data.y)) + 1
    args.num_class = num_classes

    print_configuration(args)

    save_path_teacher = 'weight/glnn-tea-sanitygnnmix_{}_{}_{}_{}_{}_{}_'.format(args.model, args.seed, args.add_soft, args.use_val, args.policy_layer, args.policy_hidden) + args.dataset + "_{}_{}".format(args.num_layer, args.hidden)\
                + "_{}_{}_{}_{}_{}_mlp-{}_{}_{}_{}_{}_{}_{}_{}".format(args.lr, args.weight_decay, args.dropout, args.use_norm, args.golden, args.lr_mlp, args.dropout_mlp, args.num_layer_mlp, args.hidden_mlp, args.weight_decay_mlp, args.pos_sample, args.neg_sample, args.generate) + "_model.pth"
    save_path_student = 'weight/glnn-stu-sanitygnnmix_{}_{}_{}_{}_{}_{}_'.format(args.model, args.seed, args.add_soft, args.use_val, args.policy_layer, args.policy_hidden) + args.dataset + "_{}_{}".format(args.num_layer, args.hidden)\
                + "_{}_{}_{}_{}_{}_mlp-{}_{}_{}_{}_{}_{}_{}_{}".format(args.lr, args.weight_decay, args.dropout, args.use_norm, args.golden, args.lr_mlp, args.dropout_mlp, args.num_layer_mlp, args.hidden_mlp, args.weight_decay_mlp, args.pos_sample, args.neg_sample, args.generate) + "_model.pth"

    main(data, in_feat, num_classes, args, save_path_teacher, save_path_student, device)
