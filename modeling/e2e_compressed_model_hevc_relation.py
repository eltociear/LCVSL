import itertools
import time

import math

import einops
import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.nn import Module
from torchvision import models
from torchvision.ops import DeformConv2d, FrozenBatchNorm2d
from transformers import BertConfig, BertLayer
import mv_warp_func_gpu

from utils.distribute import is_main_process

GOP = 4
INDEX = 0


def prepare_gaussian_targets(targets, sigma=1):
    gaussian_targets = []
    for batch_idx in range(targets.shape[0]):
        t = targets[batch_idx]
        axis = torch.arange(len(t), device=targets.device)
        gaussian_t = torch.zeros_like(t)
        indices, = torch.nonzero(t, as_tuple=True)
        for i in indices:
            g = torch.exp(-(axis - i) ** 2 / (2 * sigma * sigma))
            gaussian_t += g

        gaussian_t = gaussian_t.clamp(0, 1)
        # gaussian_t /= gaussian_t.max()
        gaussian_targets.append(gaussian_t)
    gaussian_targets = torch.stack(gaussian_targets, dim=0)
    return gaussian_targets


def cosine_compare(inputs, similarity_module, k):
    """(b c t)"""
    B = inputs.shape[0]
    L = inputs.shape[-1]

    padded_inputs = F.pad(inputs, pad=(0, math.ceil(L / k) * k - L), mode='replicate')
    # pad_L = padded_inputs.shape[-1]

    outputs = torch.zeros_like(padded_inputs)
    for offset in range(k):
        left_x = F.pad(padded_inputs, pad=(k - offset, 0), mode='replicate')[:, :, :-(k - offset)]
        right_x = F.pad(padded_inputs, pad=(0, offset + 1), mode='replicate')[:, :, (offset + 1):]
        left_seq = einops.rearrange(left_x, 'b c (k nw) -> (b nw) c k', k=k)
        right_seq = einops.rearrange(right_x, 'b c (k nw) -> (b nw) c k', k=k)

        h = similarity_module(left_seq, right_seq, padded_inputs, offset)  # (b nw) c
        hidden_state = einops.rearrange(h, '(b nw) c -> b c nw', b=B)

        outputs[:, :, offset::k] = hidden_state

    outputs = einops.rearrange(outputs[:, :, :L], 'b c t -> b t c')  # (b t c)
    return outputs


class SimilarityModule(Module):
    def __init__(self, dim, k):
        super().__init__()
        self.conv1 = nn.Conv1d(dim, dim, kernel_size=k)
        self.conv2 = nn.Conv1d(dim, dim, kernel_size=k)
        self.merge = nn.Linear(dim * 2, dim)

    def forward(self, left_seq, right_seq, x, offset):
        """
        Args:
            left_seq: (b nw) c k
            right_seq: (b nw) c k
            x: (b c t)
            offset: int
        Returns:
        """
        feats1 = self.conv1(left_seq).squeeze(-1)
        feats2 = self.conv2(right_seq).squeeze(-1)

        return self.merge(torch.cat([feats1, feats2], dim=1))


class LeftRightFeatureExtractor(Module):
    def __init__(self, dim, stride, kernel_size):
        super().__init__()
        self.kernel_size = kernel_size
        self.left_conv = nn.Conv1d(dim, dim, kernel_size=kernel_size, stride=stride)
        self.right_conv = nn.Conv1d(dim, dim, kernel_size=kernel_size, stride=stride)

        # self.similarity_module = SimilarityModule(dim, kernel_size)

    def forward(self, x):
        """x: (b c t)"""
        left_feats = self.left_conv(F.pad(x, pad=(self.kernel_size, 0), mode='replicate')[:, :, :-1])
        right_feats = self.right_conv(F.pad(x, pad=(0, self.kernel_size), mode='replicate')[:, :, 1:])

        feats = torch.cat([left_feats, right_feats], dim=1)  # (b c t)
        # feats = self.bn1(left_feats) + self.bn2(right_feats)

        # feats = cosine_compare(x, self.similarity_module, self.kernel_size)  # (b t c)

        return feats


class FPN(Module):
    def __init__(self, in_channels, dim):
        super().__init__()
        self.layer_block = nn.Conv2d(dim, dim, kernel_size=3, padding=1)
        self.inner_blocks = nn.ModuleList()
        for c in in_channels:
            self.inner_blocks.append(nn.Conv2d(c, dim, 1), )

    def forward(self, features_list):
        target_idx = 1
        result_feature = self.inner_blocks[target_idx](features_list[target_idx])
        for idx, feature in enumerate(features_list):
            if idx != target_idx:
                feature = self.inner_blocks[idx](feature)
                feature = F.interpolate(feature, size=result_feature.shape[-2:], mode='bilinear', align_corners=False)
                result_feature += feature

        result_feature = self.layer_block(result_feature)
        return result_feature


class SidedataModel(Module):
    def __init__(self, cfg, dim, mode='res'):
        super().__init__()
        assert mode in ['res', 'mv', 'rgb', 'speed', 'cat']
        assert cfg.INPUT.USE_SIDE_DATA
        self.cfg = cfg

        kwargs = {'pretrained': True}
        if 'resnet' in cfg.MODEL.BACKBONE.SIDE_DATA_NAME:
            kwargs['norm_layer'] = FrozenBatchNorm2d
        self.backbone = getattr(models, cfg.MODEL.BACKBONE.SIDE_DATA_NAME)(**kwargs)

        ref_channel = 0

        if 'mobilenet' in cfg.MODEL.BACKBONE.SIDE_DATA_NAME:
            self.backbone.features[0][0] = nn.Conv2d(4 + 3 + ref_channel, 32, kernel_size=(3, 3), stride=(2, 2), padding=(1, 1), bias=False)
            self.out_features = self.backbone.classifier[-1].in_features
            del self.backbone.classifier

        elif 'shufflenet' in cfg.MODEL.BACKBONE.SIDE_DATA_NAME:
            self.backbone.conv1[0] = nn.Conv2d(4 + 3 + ref_channel, 24, kernel_size=(3, 3), stride=(2, 2), padding=(1, 1), bias=False)
            self.out_features = self.backbone.fc.in_features
            del self.backbone.fc

        else:  # resnet
            self.out_features = self.backbone.fc.in_features
            del self.backbone.fc

            if mode == 'mv':
                setattr(self.backbone, 'conv1', nn.Conv2d(4, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False))
                self.bn = nn.BatchNorm2d(4)
            elif mode == 'cat':
                setattr(self.backbone, 'conv1', nn.Conv2d(4 + 3 + ref_channel, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False))
                self.bn = nn.BatchNorm2d(4 + 3 + ref_channel)
            elif mode == 'speed':
                setattr(self.backbone, 'conv1', nn.Conv2d(2, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False))
                self.bn = nn.BatchNorm2d(2)
            elif mode == 'res':
                self.bn = nn.BatchNorm2d(3)
            elif mode == 'rgb':
                self.bn = nn.Identity()

        self.embedding = nn.Conv2d(self.out_features, dim, 3, 1, 1)

    def forward(self, x):
        mv_feature = None
        if 'mobilenet' in self.cfg.MODEL.BACKBONE.SIDE_DATA_NAME:
            x = self.backbone.features(x)
        elif 'shufflenet' in self.cfg.MODEL.BACKBONE.SIDE_DATA_NAME:
            x = self.backbone.conv1(x)
            x = self.backbone.maxpool(x)
            x = self.backbone.stage2(x)
            x = self.backbone.stage3(x)
            x = self.backbone.stage4(x)
            x = self.backbone.conv5(x)
        else:
            x = self.bn(x)
            x = self.backbone.conv1(x)
            x = self.backbone.bn1(x)
            x = self.backbone.relu(x)
            x = self.backbone.maxpool(x)

            x = self.backbone.layer1(x)  # 64, 56, 56
            mv_feature = x
            x = self.backbone.layer2(x)  # 128, 28, 28
            x = self.backbone.layer3(x)  # 256, 14, 14
            x = self.backbone.layer4(x)  # 512, 7, 7

        outputs = self.embedding(x)
        return outputs, mv_feature


class EstimatorDenseNetTiny(nn.Module):
    def __init__(self, ch_in, ch_out):
        super(EstimatorDenseNetTiny, self).__init__()

        def Conv2D(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
            return nn.Sequential(
                nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation, bias=True),
                nn.LeakyReLU(0.1)
            )

        self.conv0 = Conv2D(ch_in, 8, kernel_size=3, stride=1)
        dd = 8
        self.conv1 = Conv2D(ch_in + dd, 8, kernel_size=3, stride=1)
        dd += 8
        self.conv2 = Conv2D(ch_in + dd, 6, kernel_size=3, stride=1)
        dd += 6
        self.conv3 = Conv2D(ch_in + dd, 4, kernel_size=3, stride=1)
        dd += 4
        self.conv4 = Conv2D(ch_in + dd, 2, kernel_size=3, stride=1)
        dd += 2
        self.predict_flow = nn.Conv2d(ch_in + dd, ch_out, kernel_size=3, stride=1, padding=1, bias=True)

    def forward(self, x):
        x = torch.cat((self.conv0(x), x), 1)
        x = torch.cat((self.conv1(x), x), 1)
        x = torch.cat((self.conv2(x), x), 1)
        x = torch.cat((self.conv3(x), x), 1)
        x = torch.cat((self.conv4(x), x), 1)
        return self.predict_flow(x)


class UpsampleUpdatingModel2(Module):
    def __init__(self, cfg, dim, mode='mv'):
        super().__init__()
        self.mode = mode
        self._use_gan = cfg.MODEL.USE_GAN
        self.backbone = SidedataModel(cfg, dim, mode=mode)
        self.channel_weight_predictor = nn.Sequential(
            nn.Linear(dim, dim),
            nn.ReLU(),
            nn.Linear(dim, dim),
            nn.Sigmoid(),
        )
        in_dim = {
            'mv': 2, 'res': 3
        }[mode]

        self.spatial_module = EstimatorDenseNetTiny(in_dim + dim * 2, 1)
        self.channel_module = EstimatorDenseNetTiny(in_dim + dim * 2, dim)

        # self.motion_convs = nn.Sequential(
        #     nn.Conv2d(dim, dim, kernel_size=3, padding=1),
        #     nn.ReLU(inplace=True),
        #     nn.Conv2d(dim, dim, kernel_size=3, padding=1),
        #     nn.ReLU(inplace=True),
        #     nn.Conv2d(dim, dim, kernel_size=3, padding=1),
        #     nn.ReLU(inplace=True)
        # )

    def forward(self, imgs, i_features, p_motions):
        """
        Args:
            imgs: (4, 100, 3, 224, 224)
            i_features: (100, 256, 7, 7)
            p_motions: (100, 3, 2, 224, 224)
        Returns:
        """
        B = imgs.shape[0]
        num_gop = imgs.shape[1] // GOP
        i_features_o = i_features = i_features.unsqueeze(1).expand(-1, GOP - 1, -1, -1, -1).reshape(-1, *i_features.shape[-3:])  # (bn gop) c h w

        p_motions = einops.rearrange(p_motions, 'bn gop c h w -> (bn gop) c h w')
        p_features = self.backbone.extract_features(p_motions)

        p_motions_resized = F.interpolate(p_motions, size=p_features.shape[-2:], mode='bilinear', align_corners=False)

        channel_weight = self.channel_module(torch.cat([p_motions_resized, p_features, i_features], dim=1))
        weight = self.channel_weight_predictor(F.adaptive_avg_pool2d(channel_weight, 1).flatten(1))  # (300, 256)
        # weight = self.channel_weight_predictor(F.adaptive_max_pool2d(channel_weight, 1).flatten(1))  # (300, 256)

        i_features = i_features * weight.unsqueeze(-1).unsqueeze(-1)  # (bn gop) c h w

        spatial_weight = self.spatial_module(torch.cat([p_motions_resized, p_features, i_features], dim=1))
        spatial_weight = F.softmax(spatial_weight.view(*spatial_weight.shape[:2], -1), dim=-1).view_as(spatial_weight)
        i_features = (i_features * spatial_weight).sum(dim=(2, 3))  # (bn gop) c

        p_features = i_features + F.adaptive_avg_pool2d(p_features, 1).flatten(1)  # (bn gop) c
        p_features = einops.rearrange(p_features, '(b n t) c -> b n t c', b=B, n=num_gop)  # b n k c

        return p_features


class TemporalModule(nn.Module):
    def __init__(self, cfg, dim, kernel_size=8, out_dim=None):
        super().__init__()
        k_size = 8
        print(f'k={k_size}')
        # self.extractor = LeftRightFeatureExtractor(dim, stride=1, kernel_size=kernel_size)
        self.extractors = nn.ModuleList([
            LeftRightFeatureExtractor(dim, stride=1, kernel_size=k) for k in [k_size]
        ])

        self.out = None
        if out_dim is not None and out_dim != dim * 2:
            self.out = nn.Linear(dim * 2, out_dim)

    def forward(self, feats):
        """
        Args:
            feats: (b c t)
        """

        feats_list = []
        for extractor in self.extractors:
            feats_list.append(extractor(feats))
        feats = sum(feats_list) / len(feats_list)  # b c t
        # feats = self.extractor(feats)  # b c t
        if self.out is not None:
            feats = self.out(feats)
        return feats


class PositionalEncoding(nn.Module):
    def __init__(self, dim, max_len=8):
        super(PositionalEncoding, self).__init__()
        # self.dropout = nn.Dropout(p=dropout)
        pe = torch.zeros(max_len, dim)  # (T, C)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, dim, 2).float() * (-math.log(10000.0) / dim))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)  # (1, T, C)
        self.register_buffer('pe', pe)

    def forward(self, x):
        x = x + self.pe
        return x


def SPoS(inputs, temporal_module, k):
    """(b c t)"""
    B = inputs.shape[0]
    L = inputs.shape[-1]
    C = inputs.shape[1]

    padded_inputs = F.pad(inputs, pad=(0, math.ceil(L / k) * k - L), mode='replicate')
    pad_L = padded_inputs.shape[-1]

    # outputs = torch.zeros_like(padded_inputs)
    outputs = torch.zeros(B, temporal_module.out_channels, pad_L, dtype=inputs.dtype, device=inputs.device)
    for offset in range(k):
        left_x = F.pad(padded_inputs, pad=(k - offset, 0), mode='replicate')[:, :, :-(k - offset)]
        right_x = F.pad(padded_inputs, pad=(0, offset + 1), mode='replicate')[:, :, (offset + 1):]
        left_seq = einops.rearrange(left_x, 'b c (nw k) -> (b nw) k c', k=k)
        right_seq = einops.rearrange(right_x, 'b c (nw k) -> (b nw) k c', k=k)
        mid_seq = einops.rearrange(padded_inputs[:, :, offset::k], 'b c nw -> (b nw) 1 c')

        h = temporal_module(left_seq, mid_seq, right_seq)  # (b nw) c
        hidden_state = einops.rearrange(h, '(b nw) c -> b c nw', b=B)

        outputs[:, :, offset::k] = hidden_state

    outputs = outputs[:, :, :L]  # (b c t)
    return outputs


class BasicConv2d(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, **kwargs):
        super(BasicConv2d, self).__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, bias=False, **kwargs)
        self.bn = nn.BatchNorm2d(out_channels)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        return F.relu(x)


class GroupSimilarity(nn.Module):
    def __init__(self, dim, window_size, group=4, similarity_func='cosine', offset=0):
        super(GroupSimilarity, self).__init__()
        self.out_channels = dim * 1
        self.group = group
        self.similarity_func = similarity_func
        self.offset = offset

        k = 5
        padding = (k - 1) // 2

        self.fcn = nn.Sequential(
            BasicConv2d(self.group, dim, kernel_size=k, stride=1, padding=padding),
            BasicConv2d(dim, dim, kernel_size=k, stride=1, padding=padding),
            BasicConv2d(dim, dim, kernel_size=k, stride=1, padding=padding),
            BasicConv2d(dim, dim, kernel_size=k, stride=1, padding=padding),
        )

        # self.pe = PositionalEncoding(dim, max_len=window_size * 2 + 1)
        # encoder_config = BertConfig(
        #     hidden_size=dim,
        #     num_attention_heads=4,
        #     intermediate_size=dim * 2,
        # )
        # self.encoders = nn.ModuleList([BertLayer(encoder_config) for _ in range(3)])
        # self.encoders = nn.LSTM(dim, dim, batch_first=True, num_layers=2)
        self.encoders = nn.GRU(dim, dim, batch_first=True, num_layers=2)

        print('sim k={}, similarity-head: {} top2-224-aug'.format(k, self.group))

    def recognize_patterns(self, left_seq, mid_seq, right_seq, offset=0):
        k = left_seq.shape[1]
        assert k > offset

        left_seq = left_seq[:, offset:]
        right_seq = right_seq[:, :(None if offset == 0 else -offset)]
        assert left_seq.shape[1] == right_seq.shape[1] == (k - offset)

        x = torch.cat([left_seq, mid_seq, right_seq], dim=1)
        # x = self.pe(x)
        # for encoder in self.encoders:
        #     x = encoder(x)[0]  # (B, L, C)
        x, (_, _) = self.encoders(x)

        # x = self.linear(x)
        B, L, C = x.shape
        x = x.view(B, L, self.group, C // self.group)  # (B, L, G, C')
        # (B, L, L, H)
        similarity_func = self.similarity_func

        if similarity_func == 'cosine':
            sim = F.cosine_similarity(x.unsqueeze(2), x.unsqueeze(1), dim=-1)  # batch, T, T, G
        else:
            raise NotImplemented

        sim = sim.permute(0, 3, 1, 2)  # batch, G, T, T

        # print(sim.shape)
        # import numpy as np
        # global INDEX
        # np.save(f'similarity_maps{INDEX}', sim.detach().cpu().numpy())
        # INDEX += 1

        h = self.fcn(sim)  # batch, dim, T, T
        h = F.adaptive_avg_pool2d(h, 1).flatten(1)

        return h

    def forward(self, left_seq, mid_seq, right_seq):
        """
        left_seq = batch, T, dim
        mid_seq = batch, 1, dim
        right_seq = batch, T, dim
        """
        h = self.recognize_patterns(left_seq, mid_seq, right_seq, offset=self.offset)

        return h


class EstimatorDenseNetTiny2(nn.Module):
    def __init__(self, ch_in, ch_out):
        super().__init__()

        def Conv2D(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
            return nn.Sequential(
                nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation, bias=True),
                nn.LeakyReLU(0.1)
            )

        self.conv0 = Conv2D(ch_in, 8, kernel_size=3, stride=1)
        dd = 8
        self.conv1 = Conv2D(ch_in + dd, 8, kernel_size=3, stride=1)
        dd += 8
        self.conv2 = Conv2D(ch_in + dd, 6, kernel_size=3, stride=1)
        dd += 6
        self.conv3 = Conv2D(ch_in + dd, 4, kernel_size=3, stride=1)
        dd += 4
        self.conv4 = Conv2D(ch_in + dd, 2, kernel_size=3, stride=1)
        dd += 2
        self.predict_flow = nn.Conv2d(ch_in + dd, ch_out, kernel_size=3, stride=1, padding=1, bias=True)

    def forward(self, x):
        x = torch.cat((self.conv0(x), x), 1)
        x = torch.cat((self.conv1(x), x), 1)
        x = torch.cat((self.conv2(x), x), 1)
        x = torch.cat((self.conv3(x), x), 1)
        x = torch.cat((self.conv4(x), x), 1)
        return self.predict_flow(x)


class CBAM(Module):
    def __init__(self, dim):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(dim, dim // 16),
            nn.ReLU(),
            nn.Linear(dim // 16, dim)
        )
        self.spatial_module = EstimatorDenseNetTiny(dim + dim * 1, 1)
        self.channel_module = EstimatorDenseNetTiny(dim + dim * 1, dim)

    def forward(self, i_feats, mv_feats):
        """
        Args:
            i_feats:  N, C, H, W
            mv_feats: N, C, H, W
        Returns:
        """
        channel_weight = self.channel_module(torch.cat([i_feats, mv_feats], dim=1))
        channel_att = self.mlp(F.adaptive_avg_pool2d(channel_weight, 1).flatten(1))
        i_feats = i_feats * channel_att.sigmoid().unsqueeze(-1).unsqueeze(-1)

        spatial_weight = self.spatial_module(torch.cat([i_feats, mv_feats], dim=1))
        spatial_weight = F.softmax(spatial_weight.view(*spatial_weight.shape[:2], -1), dim=-1).view_as(spatial_weight)
        i_feats = (i_feats * spatial_weight).sum(dim=(2, 3))  # (bn gop) c
        p_features = i_feats + F.adaptive_avg_pool2d(mv_feats, 1).flatten(1)  # (bn gop) c
        return p_features


class E2ECompressedGEBDModel(Module):
    def __init__(self, cfg):
        super().__init__()
        # assert cfg.INPUT.USE_SIDE_DATA
        self._use_gan = cfg.MODEL.USE_GAN
        self._use_residual = cfg.MODEL.USE_RESIDUAL
        self._use_mv_as_deconv_params = cfg.MODEL.USE_MV_AS_DECONV_PARAMS

        if is_main_process():
            print('USE_GAN:', self._use_gan)
            print('USE_RESIDUAL:', self._use_residual)
            print('USE_MV_AS_DECONV_PARAMS:', self._use_mv_as_deconv_params)

        self.backbone_name = cfg.MODEL.BACKBONE.NAME
        if self.backbone_name == 'csn':
            from .backbone import CSN
            self.backbone = CSN()
            in_feat_dim = 2048
        elif self.backbone_name == 'tsn':
            from .backbone import TSN
            self.backbone = TSN()
            in_feat_dim = 2048
        else:
            # self.backbone = getattr(models, cfg.MODEL.BACKBONE.NAME)(pretrained=True, norm_layer=FrozenBatchNorm2d)
            # for param in itertools.chain(self.backbone.conv1.parameters(), self.backbone.bn1.parameters()):
            #     param.requires_grad = False
            # in_feat_dim = self.backbone.fc.in_features
            # del self.backbone.fc
            pass

        self.kernel_size = cfg.MODEL.KERNEL_SIZE
        dim = 256
        self.dim = dim

        # self.mv_backbone = SidedataModel(cfg, dim, mode='mv')
        # self.ref_mv_backbone = SidedataModel(cfg, dim, mode='mv')
        # self.res_backbone = SidedataModel(cfg, dim, mode='res')
        self.cat_backbone = SidedataModel(cfg, dim, mode='cat')
        # self.speed_backbone = SidedataModel(cfg, dim, mode='speed')
        # self.cbam = CBAM(dim)

        self.temporal_module = GroupSimilarity(dim=dim,
                                               window_size=self.kernel_size,
                                               group=4,
                                               similarity_func='cosine')

        self.classifier = nn.Sequential(
            nn.Conv1d(self.temporal_module.out_channels, dim, 3, 1, 1),
            nn.PReLU(),
            nn.Conv1d(dim, dim, 3, 1, 1),
            nn.PReLU(),
            nn.Conv1d(dim, 1, 1)
        )

        self.relation_conv = nn.Sequential(
            nn.Conv2d(dim, dim, 3, 2, 1),
            nn.ReLU(),
            nn.Conv2d(dim, dim, 3, 2, 1),
            nn.ReLU(),
            nn.MaxPool2d(3, 2, 1)
        )

        self.mv_feature_embedding = nn.Conv2d(64, dim, 1)

        # FPN
        # self.fpn = FPN([256, 512, 1024, 2048], dim)
        # self.embedding = nn.Conv2d(2048, dim, 3, 1, 1)
        # self.pe = PositionEmbeddingSine(dim, normalize=True)

    def extract_features(self, x):
        x = self.backbone.conv1(x)
        x = self.backbone.bn1(x)
        x = self.backbone.relu(x)
        x = self.backbone.maxpool(x)

        outputs = []
        x = self.backbone.layer1(x)  # (256 * 56 * 56)
        outputs.append(x)
        x = self.backbone.layer2(x)  # (512 * 28 * 28)
        outputs.append(x)
        x = self.backbone.layer3(x)  # (1024 * 14 * 14)
        outputs.append(x)
        x = self.backbone.layer4(x)  # (2048 * 7 * 7)
        outputs.append(x)

        # outputs = self.fpn(outputs)[1]
        outputs = self.embedding(x)
        return outputs

    def forward_feats(self, inputs, mask, module, outputs):
        inputs = inputs.view(-1, *inputs.shape[-3:])[mask.view(-1)]
        features = module(inputs)
        features = F.adaptive_avg_pool2d(features, 1).flatten(1)
        dim = features.shape[-1]
        outputs.view(-1, dim)[mask.view(-1)] = features

    def forward(self, inputs, targets=None):
        """
        Args:
            inputs(dict): imgs (B, T, C, H, W);
            targets:
        Returns:
        """
        imgs = inputs['imgs']  # (4, 100, 3, 224, 224)

        mv = inputs['mv'][:, :, :4].contiguous()  # (4, 100, 4, 224, 224)
        frame_per_batch = mv.shape[1]
        # ref_mv = inputs['ref_mv'][:, :, :4].contiguous()  # (4, 100, 4, 224, 224)
        origin_mv = inputs['origin_mv'][:, :, :6]
        decode_order = inputs['decode_order']
        res = inputs['res']  # (4, 100, 3, 224, 224)
        frame_mask = inputs['frame_mask']  # (4, 100)
        rgb_frame_mask = inputs['rgb_frame_mask']  # (4, 100)
        # print(imgs.shape, mv.shape, res.shape, frame_mask.shape, rgb_frame_mask.shape)
        # start = time.time()
        B = imgs.shape[0]

        # i_imgs = imgs.view(-1, *imgs.shape[-3:])
        # i_features = self.extract_features(i_imgs)
        # i_features = F.adaptive_avg_pool2d(i_features, 1).flatten(1)
        # feats = i_features.view(*imgs.shape[:2], -1)

        # i_imgs = imgs.view(-1, *imgs.shape[-3:])
        # i_features = self.extract_features(i_imgs)
        # i_features = F.adaptive_avg_pool2d(i_features, 1).flatten(1)

        # feats = torch.zeros(np.prod(imgs.shape[:2]), dim, dtype=i_features.dtype, device=i_features.device)
        # feats[rgb_frame_indices] = i_features

        # feats.view(-1, dim)[rgb_frame_mask.view(-1) == 1] = i_features

        # mv = mv.view(-1, *mv.shape[-3:])
        # mv_features = self.mv_backbone(mv)
        # mv_features = F.adaptive_avg_pool2d(mv_features, 1).flatten(1)
        # dim = mv_features.shape[1]
        time_cost = {}
        start = time.perf_counter()
        mv = torch.cat([mv, res], dim=2)
        mv = mv.view(-1, *mv.shape[-3:])
        mv_features, mv_feature_origin = self.cat_backbone(mv)

        # torch.Size([1, 300, 3, 224, 224]) torch.Size([300, 256, 7, 7]) torch.Size([1, 300, 6, 56, 56]) torch.Size([1, 300])
        # print(imgs.shape, mv_features.shape, origin_mv.shape, decode_order.shape)
        # print(decode_order)
        # print(origin_mv)

        # mv_features_upsample = F.interpolate(mv_features, size=origin_mv.shape[-2:], mode='bilinear')
        mv_features_upsample = self.mv_feature_embedding(mv_feature_origin)
        mv_features_upsample = mv_features_upsample.view(B, -1, *mv_features_upsample.shape[-3:])
        propagation_tmp = torch.zeros_like(mv_features_upsample)

        for batch_idx in range(B):
            for i, mask in zip(decode_order[batch_idx].tolist(), frame_mask[batch_idx].tolist()):
                if mask and i != -1:
                    # torch.cuda.synchronize()
                    output_t = mv_warp_func_gpu.forward(
                        mv_features_upsample[batch_idx],
                        origin_mv[batch_idx][i][0],
                        origin_mv[batch_idx][i][1],
                        origin_mv[batch_idx][i][2],
                        origin_mv[batch_idx][i][3],
                        origin_mv[batch_idx][i][4].to(torch.int32),
                        origin_mv[batch_idx][i][5].to(torch.int32),
                        i,
                    )
                    propagation_tmp[batch_idx][i] = output_t

        propagation_tmp = propagation_tmp + mv_features_upsample - mv_features_upsample.detach()
        propagation_tmp = propagation_tmp.view(-1, *propagation_tmp.shape[-3:])
        propagation_tmp = self.relation_conv(propagation_tmp)
        # mv_features = (mv_features_upsample + propagation_tmp).view(-1, *propagation_tmp.shape[2:])
        mv_features = (mv_features + propagation_tmp)
        mv_features = F.adaptive_avg_pool2d(mv_features, 1).flatten(1)
        dim = mv_features.shape[1]
        time_cost['backbone'] = time.perf_counter() - start

        # speed = speed.view(-1, *speed.shape[-3:])
        # speed_features = self.speed_backbone(speed)
        # speed_features = F.adaptive_avg_pool2d(speed_features, 1).flatten(1)

        # res = res.view(-1, *res.shape[-3:])
        # res_features = self.res_backbone(res)
        # res_features = F.adaptive_avg_pool2d(res_features, 1).flatten(1)

        # rgb_frame_indices = torch.nonzero(rgb_frame_mask == 1, as_tuple=True)[0]
        # not_rgb_frame_indices = torch.nonzero(rgb_frame_mask != 1, as_tuple=True)[0]

        # feats = torch.zeros(np.prod(imgs.shape[:2]), dim, dtype=i_features.dtype, device=i_features.device)
        # feats.scatter_add_(1, rgb_frame_indices.unsqueeze(1), i_features)
        # feats.scatter_add_(1, not_rgb_frame_indices.unsqueeze(1), mv_features)
        # feats.scatter_add_(1, not_rgb_frame_indices.unsqueeze(1), res_features)

        # ref_mv = ref_mv.view(-1, *ref_mv.shape[-3:])
        # ref_mv_features = self.ref_mv_backbone(ref_mv)
        # ref_mv_features = F.adaptive_avg_pool2d(ref_mv_features, 1).flatten(1)
        # ref_mv_features = ref_mv_features.view(B, -1, *ref_mv_features.shape[-3:])

        # i_feats_list = []
        # for batch_idx in range(B):
        #     mask = rgb_frame_mask[batch_idx] == 1
        #     i_imgs = imgs[batch_idx][mask]
        #     i_feats = self.extract_features(i_imgs)
        #     indices = torch.nonzero(mask, as_tuple=True)[0].tolist()
        #
        #     ref_indices = torch.zeros(len(mask), dtype=torch.int64, device=i_feats.device)
        #     for i, (s, e) in enumerate(zip(indices, indices[1:] + [None])):
        #         ref_indices[s:e] = i
        #
        #     # i_feats = self.cbam(i_feats[ref_indices], ref_mv_features[batch_idx])
        #     # i_feats = F.adaptive_avg_pool2d(i_feats, 1).flatten(1)
        #     i_feats_list.append(i_feats)
        #
        # i_feats = torch.stack(i_feats_list, dim=0).view(-1, dim)

        # rgb_frame_mask = rgb_frame_mask.float().unsqueeze(-1)
        # feats = i_features * rgb_frame_mask + (mv_features + res_features) * (1.0 - rgb_frame_mask)
        feats = mv_features
        start = time.perf_counter()
        feats = einops.rearrange(feats, '(b t) c -> b c t', b=B)  # (4, 512, 100)
        feats = SPoS(feats, self.temporal_module, self.kernel_size)  # b c t
        logits = self.classifier(feats)  # b 1 t

        if self.training:
            targets = targets.to(logits.dtype)
            gaussian_targets = prepare_gaussian_targets(targets)
            frame_mask = frame_mask.view(-1) == 1

            loss = F.binary_cross_entropy_with_logits(logits.view(-1)[frame_mask], gaussian_targets.view(-1)[frame_mask])
            loss_dict = {'loss': loss}

            return loss_dict
        scores = torch.sigmoid(logits).flatten(1)
        time_cost['head'] = time.perf_counter() - start
        return scores, time_cost