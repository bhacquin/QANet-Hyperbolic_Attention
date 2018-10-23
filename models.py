import torch
import torch.nn as nn
import torch.nn.functional as F
import math
import numpy as np
from config import config, device

D = config.connector_dim
Nh = config.num_heads
Dword = config.glove_dim
Dchar = config.char_dim
batch_size = config.batch_size
dropout = config.dropout
dropout_char = config.dropout_char

Lc = config.para_limit
Lq = config.ques_limit


def arccosh(x):
    # log(x + sqrt(x^2 -1)
    # log(x (1 + sqrt(x^2 -1)/x))
    # log(x) + log(1 + sqrt(x^2 -1)/x)
    # log(x) + log(1 + sqrt((x^2 -1)/x^2))
    # log(x) + log(1 + sqrt(1 - x^-2))
    x = x + 1e-6
    c0 = torch.log(x)
    c1 = torch.log1p(torch.sqrt(x * x - 1) / x)
    return c0 + c1

def mask_logits(inputs, mask):
    mask = mask.type(torch.float32)
    return inputs + (-1e30) * (1 - mask)

class Initialized_Conv1d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=1, relu=False, stride=1, padding=0, groups=1, bias=False):
        super().__init__()
        self.out = nn.Conv1d(in_channels, out_channels, kernel_size, stride=stride, padding=padding, groups=groups, bias=bias)
        if relu is True:
            self.relu = True
            nn.init.kaiming_normal_(self.out.weight, nonlinearity='relu')
        else:
            self.relu = False
            nn.init.xavier_uniform_(self.out.weight)

    def forward(self, x):
        if self.relu == True:
            return F.relu(self.out(x))
        else:
            return self.out(x)

def PosEncoder(x, min_timescale=1.0, max_timescale=1.0e4):
    x = x.transpose(1,2)
    length = x.size()[1]
    channels = x.size()[2]
    signal = get_timing_signal(length, channels, min_timescale, max_timescale)
    return (x + signal.cuda()).transpose(1,2)

def get_timing_signal(length, channels, min_timescale=1.0, max_timescale=1.0e4):
    position = torch.arange(length).type(torch.float32)
    num_timescales = channels // 2
    log_timescale_increment = (math.log(float(max_timescale) / float(min_timescale)) / (float(num_timescales)-1))
    inv_timescales = min_timescale * torch.exp(
            torch.arange(num_timescales).type(torch.float32) * -log_timescale_increment)
    scaled_time = position.unsqueeze(1) * inv_timescales.unsqueeze(0)
    signal = torch.cat([torch.sin(scaled_time), torch.cos(scaled_time)], dim = 1)
    m = nn.ZeroPad2d((0, (channels % 2), 0, 0))
    signal = m(signal)
    signal = signal.view(1, length, channels)
    return signal

class DepthwiseSeparableConv(nn.Module):
    def __init__(self, in_ch, out_ch, k, bias=True):
        super().__init__()
        self.depthwise_conv = nn.Conv1d(in_channels=in_ch, out_channels=in_ch, kernel_size=k, groups=in_ch, padding=k // 2, bias=False)
        self.pointwise_conv = nn.Conv1d(in_channels=in_ch, out_channels=out_ch, kernel_size=1, padding=0, bias=bias)
    def forward(self, x):
        return F.relu(self.pointwise_conv(self.depthwise_conv(x)))


class Highway(nn.Module):
    def __init__(self, layer_num: int, size=D):
        super().__init__()
        self.n = layer_num
        self.linear = nn.ModuleList([Initialized_Conv1d(size, size, relu=False, bias=True) for _ in range(self.n)])
        self.gate = nn.ModuleList([Initialized_Conv1d(size, size, bias=True) for _ in range(self.n)])

    def forward(self, x):
        #x: shape [batch_size, hidden_size, length]
        for i in range(self.n):
            gate = torch.sigmoid(self.gate[i](x))
            nonlinear = self.linear[i](x)
            nonlinear = F.dropout(nonlinear, p=dropout, training=self.training)
            x = gate * nonlinear + (1 - gate) * x
            #x = F.relu(x)
        return x


class SelfAttention(nn.Module):
    def __init__(self):
        super().__init__()
        self.mem_conv = Initialized_Conv1d(D, D*2, kernel_size=1, relu=False, bias=False)
        self.query_conv = Initialized_Conv1d(D, D, kernel_size=1, relu=False, bias=False)
        self.att_conv = Initialized_Conv1d(400, 400, kernel_size=1, relu=False, bias=False)
        bias = torch.empty(1)
        beta = torch.empty(1)
        self.norm_01 = nn.LayerNorm(D//Nh)
        self.norm_0 = nn.LayerNorm(D//Nh +1)
        self.norm_00 = nn.LayerNorm(D//Nh +1)
        nn.init.constant_(bias, 0)
        self.bias = nn.Parameter(bias)
        self.beta = nn.Parameter(beta)

    def forward(self, queries, mask):
        memory = queries

        memory = self.mem_conv(memory)
        query = self.query_conv(queries)
        memory = memory.transpose(1, 2)
        query = query.transpose(1, 2)
        Q = self.split_last_dim(query, Nh)
        K, V = [self.split_last_dim(tensor, Nh) for tensor in torch.split(memory, D, dim=2)]

        key_depth_per_head = D // Nh
        Q *= key_depth_per_head**-0.5
        # x = self.dot_product_attention(Q, K, V, mask = mask)
        x = self.hyperbolic_attention(Q, K, V, mask = mask)
        return self.combine_last_two_dim(x.permute(0,2,1,3)).transpose(1, 2)

    def proj_hyperboloide(self,q):

        q_norm = q.norm(dim=-1)

        # if (q_norm.detach() ==  float('inf')).any():
        #     print('norme infinie')
        # if np.isnan(q_norm.detach()).any():
        #     print('norme nan')

        # q_normalized = (q.transpose(-2,-1) / q_norm.unsqueeze(-2)).transpose(-2,-1)
        q_normalized = F.normalize(q, dim=-1)
        # if (q_normalized.detach() ==  float('inf')).any():
        #     print('q_normalized infini')
        cosh_q = torch.cosh(q_norm)
        # if (cosh_q.detach() ==  float('inf')).any():
        #     print('cosh_q infini')
        sinh_q = torch.sinh(q_norm)
        # if (sinh_q.detach() ==  float('inf')).any():
        #     print('sinh_q infini')
        abs_q = (q_normalized.transpose(-2,-1)*sinh_q.unsqueeze(-2))
        q_hyper = torch.cat((abs_q,cosh_q.unsqueeze(-2)), dim=-2).transpose(-2,-1)
        return q_hyper

    def proj_klein(self,q):
        q_hyper_ = self.proj_hyperboloide(q)
        q_klein_ =(q_hyper_.transpose(-2,-1)/q_hyper_[...,q.size(-1)].unsqueeze(-2)).transpose(-2,-1).narrow(-1,0,q.size(-1))
        # if (q_klein_.detach() ==  float('inf')).any():
        #     print('q_klein_ infini')
        # if np.isnan(q_klein_.detach()).any():
        #     print('nan q_klein_')
        return F.normalize(q_klein_, dim=-1)

    def hyperbolic_scalar_product(self,q_hyper,c_hyper,input_size):
        q_n1 = q_hyper[...,input_size]
        q_n = q_hyper[...,:input_size]
        c_n1 = c_hyper[...,input_size]
        c_n = c_hyper[...,:input_size]
        c_nq_n = torch.matmul(c_n,q_n.transpose(-2,-1))
        c_n1_q_n1 = torch.matmul(c_n1.unsqueeze(-1), q_n1.unsqueeze(-1).transpose(-2,-1))
        diag = torch.diag(torch.ones(q_hyper.size(-2))).unsqueeze(0).unsqueeze(0)
        ones = torch.zeros_like(c_nq_n)
        mask = ones.cuda()+diag.cuda()
        mask = mask.byte()
        out = c_nq_n - c_n1_q_n1
        if (q_hyper.detach() == c_hyper.detach()).all() :
            out = out.masked_fill_(mask, -1.)
        return out

    def hyperbolic_distance(self,q_hyper,c_hyper,input_size):
        # print((-self.hyperbolic_scalar_product(q_hyper,c_hyper,input_size)).size())

        return arccosh(-self.hyperbolic_scalar_product(q_hyper,c_hyper,input_size))

    def Lorentz_denominator(self,v_klein, eps = 1e-4):
        norm = (v_klein.norm(dim=-1) - eps) **2
        if (norm.detach() >= 1.).any():
            print('problem')
            print(norm.detach().max())
        # if np.isnan(norm.detach()).any():
        #     print('Lorentz norm ',norm)
        denom = torch.sqrt(1-norm) + eps

        tensor_ = torch.zeros_like(denom) + 1
        return (tensor_/denom).unsqueeze(-2)

    def attention_module(self,c,q,v, input_size):
        c_hyper = self.proj_hyperboloide(c)
        q_hyper = self.proj_hyperboloide(q)
        v_klein = self.proj_klein(v)
        alpha = F.softmax(self.hyperbolic_distance(q_hyper,c_hyper,input_size)*self.Lorentz_denominator(v_klein), dim=-1)
        return torch.matmul(alpha,v_klein)

    def dot_product_attention(self, q, k ,v, bias = False, mask = None):
        """dot-product attention.
        Args:
        q: a Tensor with shape [batch, heads, length_q, depth_k]
        k: a Tensor with shape [batch, heads, length_kv, depth_k]
        v: a Tensor with shape [batch, heads, length_kv, depth_v]
        bias: bias Tensor (see attention_bias())
        is_training: a bool of training
        scope: an optional string
        Returns:
        A Tensor.
        """
        logits = torch.matmul(q,k.permute(0,1,3,2))
        if bias:
            logits += self.bias
        if mask is not None:
            shapes = [x  if x != None else -1 for x in list(logits.size())]
            mask = mask.view(shapes[0], 1, 1, shapes[-1])
            logits = mask_logits(logits, mask)
        weights = F.softmax(logits, dim=-1)
        # dropping out the attention links for each of the heads
        weights = F.dropout(weights, p=dropout, training=self.training)
        return torch.matmul(weights, v)

    def hyperbolic_attention(self, q, k ,v, bias = True, mask = None):
        """dot-product attention.
        Args:
        q: a Tensor with shape [batch, heads, length_q, depth_k]
        k: a Tensor with shape [batch, heads, length_kv, depth_k]
        v: a Tensor with shape [batch, heads, length_kv, depth_v]
        bias: bias Tensor (see attention_bias())
        is_training: a bool of training
        scope: an optional string
        Returns:
        A Tensor.
        """
        input_size = q.size(-1)

        q_hyper = self.proj_hyperboloide(q)
        k_hyper = self.proj_hyperboloide(k)
        v_klein = self.proj_klein(v)
        hyperbolic_distance = self.hyperbolic_distance(q_hyper,k_hyper,input_size)
        # Version one :  attention weights are symetric but dont use einstein midpoint
        #logits = hyperbolic_distance*self.Lorentz_denominator(v_klein)
        logits = self.beta*hyperbolic_distance
        logits = -logits + torch.log(self.Lorentz_denominator(v_klein))
        # alphas_klein = alphas*self.Lorentz_denominator(v_klein)

        # logits = hyperbolic_distance*self.Lorentz_denominator(v_klein)

        # print("logits",logits.size())
        # logits = torch.matmul(q,k.permute(0,1,3,2))
        if bias:
            logits += self.bias
        if mask is not None:
            shapes = [x  if x != None else -1 for x in list(logits.size())]
            mask = mask.view(shapes[0], 1, 1, shapes[-1])
            logits = mask_logits(logits, mask)
        #Version1
        weights = F.softmax(logits, dim=-1)
        # logits = torch.exp(-logits)
        # numerator = logits*Lorentz_denominator(v_klein)
        # denominator = torch.matmul(logits, Lorentz_denominator(v_klein).transpose(-1,-2))
        # weights = (numerator/denominator)
        weights = F.dropout(weights, p=dropout, training=self.training)
        # if (weights.detach() == torch.float('inf')).any():
        #     print('weights infini')


        # print('return ', torch.matmul(weights, v_klein).size())
        # dropping out the attention links for each of the heads
        # weights = F.dropout(weights, p=dropout, training=self.training)
        if np.isnan(torch.matmul(weights, v_klein).detach()).any():
            print('max ', logits.max(dim=-1), logits.min(dim=-1))
            print('q_hyper ',q_hyper.size(),q_hyper.max(dim=-1),np.isnan(q_hyper.detach()).any())
            print('k_hyper ',k_hyper.size(),k_hyper.max(dim=-1),np.isnan(k_hyper.detach()).any())
            print('hyperbolic_distance ',hyperbolic_distance.size(),hyperbolic_distance.max(dim=-1),np.isnan(hyperbolic_distance.detach()).any())
            print('v_klein',v_klein.size(),v_klein,np.isnan(v_klein.detach()).any())
            print('self.Lorentz_denominator(v_klein)',self.Lorentz_denominator(v_klein).size(),self.Lorentz_denominator(v_klein).max(dim=-1),np.isnan(hyperbolic_distance.detach()).any())
            print('logits ',logits.size(),logits,np.isnan(logits.detach()).any())
            print('weights ',weights.size(),weights)
            print('attention',torch.matmul(weights, v_klein).size(),torch.matmul(weights, v_klein).detach())
        return torch.matmul(weights, v_klein)





    def split_last_dim(self, x, n):
        """Reshape x so that the last dimension becomes two dimensions.
        The first of these two dimensions is n.
        Args:
        x: a Tensor with shape [..., m]
        n: an integer.
        Returns:
        a Tensor with shape [..., n, m/n]
        """
        old_shape = list(x.size())
        last = old_shape[-1]
        new_shape = old_shape[:-1] + [n] + [last // n if last else None]
        ret = x.view(new_shape)
        return ret.permute(0, 2, 1, 3)
    def combine_last_two_dim(self, x):
        """Reshape x so that the last two dimension become one.
        Args:
        x: a Tensor with shape [..., a, b]
        Returns:
        a Tensor with shape [..., ab]
        """
        old_shape = list(x.size())
        a, b = old_shape[-2:]
        new_shape = old_shape[:-2] + [a * b if a and b else None]
        ret = x.contiguous().view(new_shape)
        return ret
class Embedding(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv2d = nn.Conv2d(Dchar, D, kernel_size = (1,5), padding=0, bias=True)
        nn.init.kaiming_normal_(self.conv2d.weight, nonlinearity='relu')
        self.conv1d = Initialized_Conv1d(Dword+D, D, bias=False)
        self.high = Highway(2)

    def forward(self, ch_emb, wd_emb, length):
        N = ch_emb.size()[0]
        ch_emb = ch_emb.permute(0, 3, 1, 2)
        ch_emb = F.dropout(ch_emb, p=dropout_char, training=self.training)
        ch_emb = self.conv2d(ch_emb)
        ch_emb = F.relu(ch_emb)
        ch_emb, _ = torch.max(ch_emb, dim=3)
        ch_emb = ch_emb.squeeze()

        wd_emb = F.dropout(wd_emb, p=dropout, training=self.training)
        wd_emb = wd_emb.transpose(1, 2)
        emb = torch.cat([ch_emb, wd_emb], dim=1)
        emb = self.conv1d(emb)
        emb = self.high(emb)
        return emb


class EncoderBlock(nn.Module):
    def __init__(self, conv_num: int, ch_num: int, k: int):
        super().__init__()
        self.convs = nn.ModuleList([DepthwiseSeparableConv(ch_num, ch_num, k) for _ in range(conv_num)])
        self.self_att = SelfAttention()
        self.FFN_1 = Initialized_Conv1d(ch_num, ch_num, relu=True, bias=True)
        self.FFN_2 = Initialized_Conv1d(ch_num, ch_num, bias=True)
        self.norm_C = nn.ModuleList([nn.LayerNorm(D) for _ in range(conv_num)])
        self.norm_1 = nn.LayerNorm(D)
        self.norm_2 = nn.LayerNorm(D)
        self.conv_num = conv_num
    def forward(self, x, mask, l, blks):
        total_layers = (self.conv_num+1)*blks
        out = PosEncoder(x)
        for i, conv in enumerate(self.convs):
            res = out
            out = self.norm_C[i](out.transpose(1,2)).transpose(1,2)
            if (i) % 2 == 0:
                out = F.dropout(out, p=dropout, training=self.training)
            out = conv(out)
            out = self.layer_dropout(out, res, dropout*float(l)/total_layers)
            l += 1
        res = out
        out = self.norm_1(out.transpose(1,2)).transpose(1,2)
        out = F.dropout(out, p=dropout, training=self.training)
        out = self.self_att(out, mask)
        out = self.layer_dropout(out, res, dropout*float(l)/total_layers)
        l += 1
        res = out

        out = self.norm_2(out.transpose(1,2)).transpose(1,2)
        out = F.dropout(out, p=dropout, training=self.training)
        out = self.FFN_1(out)
        out = self.FFN_2(out)
        out = self.layer_dropout(out, res, dropout*float(l)/total_layers)
        return out

    def layer_dropout(self, inputs, residual, dropout):
        if self.training == True:
            pred = torch.empty(1).uniform_(0,1) < dropout
            if pred:
                return residual
            else:
                return F.dropout(inputs, dropout, training=self.training) + residual
        else:
            return inputs + residual


class CQAttention(nn.Module):
    def __init__(self):
        super().__init__()
        w4C = torch.empty(D, 1)
        w4Q = torch.empty(D, 1)
        w4mlu = torch.empty(1, 1, D)
        nn.init.xavier_uniform_(w4C)
        nn.init.xavier_uniform_(w4Q)
        nn.init.xavier_uniform_(w4mlu)
        self.w4C = nn.Parameter(w4C)
        self.w4Q = nn.Parameter(w4Q)
        self.w4mlu = nn.Parameter(w4mlu)

        bias = torch.empty(1)
        nn.init.constant_(bias, 0)
        self.bias = nn.Parameter(bias)

    def forward(self, C, Q, Cmask, Qmask):
        C = C.transpose(1, 2)
        Q = Q.transpose(1, 2)
        batch_size_c = C.size()[0]
        S = self.trilinear_for_attention(C, Q)
        Cmask = Cmask.view(batch_size_c, Lc, 1)
        Qmask = Qmask.view(batch_size_c, 1, Lq)
        S1 = F.softmax(mask_logits(S, Qmask), dim=2)
        S2 = F.softmax(mask_logits(S, Cmask), dim=1)
        A = torch.bmm(S1, Q)
        B = torch.bmm(torch.bmm(S1, S2.transpose(1, 2)), C)
        out = torch.cat([C, A, torch.mul(C, A), torch.mul(C, B)], dim=2)
        return out.transpose(1, 2)

    def trilinear_for_attention(self, C, Q):
        C = F.dropout(C, p=dropout, training=self.training)
        Q = F.dropout(Q, p=dropout, training=self.training)
        subres0 = torch.matmul(C, self.w4C).expand([-1, -1, Lq])
        subres1 = torch.matmul(Q, self.w4Q).transpose(1, 2).expand([-1, Lc, -1])
        subres2 = torch.matmul(C * self.w4mlu, Q.transpose(1,2))
        res = subres0 + subres1 + subres2
        res += self.bias
        return res


class Pointer(nn.Module):
    def __init__(self):
        super().__init__()
        self.w1 = Initialized_Conv1d(D*2, 1)
        self.w2 = Initialized_Conv1d(D*2, 1)

    def forward(self, M1, M2, M3, mask):
        X1 = torch.cat([M1, M2], dim=1)
        X2 = torch.cat([M1, M3], dim=1)
        Y1 = mask_logits(self.w1(X1).squeeze(), mask)
        Y2 = mask_logits(self.w2(X2).squeeze(), mask)
        return Y1, Y2


class QANet(nn.Module):
    def __init__(self, word_mat, char_mat):
        super().__init__()
        if config.pretrained_char:
            self.char_emb = nn.Embedding.from_pretrained(torch.Tensor(char_mat))
        else:
            char_mat = torch.Tensor(char_mat)
            self.char_emb = nn.Embedding.from_pretrained(char_mat, freeze=False)
        self.word_emb = nn.Embedding.from_pretrained(torch.Tensor(word_mat), freeze=True)
        self.emb = Embedding()
        self.emb_enc = EncoderBlock(conv_num=4, ch_num=D, k=7)
        self.cq_att = CQAttention()
        self.cq_resizer = Initialized_Conv1d(D * 4, D)
        self.model_enc_blks = nn.ModuleList([EncoderBlock(conv_num=2, ch_num=D, k=5) for _ in range(7)])
        self.out = Pointer()

    def forward(self, Cwid, Ccid, Qwid, Qcid):
        maskC = (torch.zeros_like(Cwid) != Cwid).float()
        maskQ = (torch.zeros_like(Qwid) != Qwid).float()
        Cw, Cc = self.word_emb(Cwid), self.char_emb(Ccid)
        Qw, Qc = self.word_emb(Qwid), self.char_emb(Qcid)
        C, Q = self.emb(Cc, Cw, Lc), self.emb(Qc, Qw, Lq)
        Ce = self.emb_enc(C, maskC, 1, 1)
        Qe = self.emb_enc(Q, maskQ, 1, 1)
        X = self.cq_att(Ce, Qe, maskC, maskQ)
        M0 = self.cq_resizer(X)
        M0 = F.dropout(M0, p=dropout, training=self.training)
        for i, blk in enumerate(self.model_enc_blks):
             M0 = blk(M0, maskC, i*(2+2)+1, 7)
        M1 = M0
        for i, blk in enumerate(self.model_enc_blks):
             M0 = blk(M0, maskC, i*(2+2)+1, 7)
        M2 = M0
        M0 = F.dropout(M0, p=dropout, training=self.training)
        for i, blk in enumerate(self.model_enc_blks):
             M0 = blk(M0, maskC, i*(2+2)+1, 7)
        M3 = M0
        p1, p2 = self.out(M1, M2, M3, maskC)
        return p1, p2
