import math
import torch
from torch import nn
from torch.nn import functional as F
import numpy as np
from .models import register_meta_arch, make_backbone, make_neck, make_generator
from .blocks import MaskedConv1D, Scale, LayerNorm
from .losses import ctr_diou_loss_1d, sigmoid_focal_loss, ctr_giou_loss_1d

from ..utils import batched_nms
class PtTransformerClsHead(nn.Module):
    """
    1D Conv heads for classification
    """
    def __init__(
        self,
        input_dim,
        feat_dim,
        num_classes,
        prior_prob=0.01,
        num_layers=3,
        kernel_size=3,
        act_layer=nn.ReLU,
        with_ln=False,
        empty_cls = [],
        detach_feat = False
    ):
        super().__init__()
        self.act = act_layer()
        self.detach_feat = detach_feat

        # build the head
        self.head = nn.ModuleList()
        self.norm = nn.ModuleList()
        for idx in range(num_layers-1):
            if idx == 0:
                in_dim = input_dim
                out_dim = feat_dim
            else:
                in_dim = feat_dim
                out_dim = feat_dim
            self.head.append(
                MaskedConv1D(
                    in_dim, out_dim, kernel_size,
                    stride=1,
                    padding=kernel_size//2,
                    bias=(not with_ln)
                )
            )
            if with_ln:
                self.norm.append(LayerNorm(out_dim))
            else:
                self.norm.append(nn.Identity())

        # classifier
        self.cls_head = MaskedConv1D(
                feat_dim,  num_classes, kernel_size,
                stride=1, padding=kernel_size//2
            )

        # use prior in model initialization to improve stability
        # this will overwrite other weight init
        if prior_prob > 0:
            bias_value = -(math.log((1 - prior_prob) / prior_prob))
            torch.nn.init.constant_(self.cls_head.conv.bias, bias_value)

        # a quick fix to empty categories:
        # the weights assocaited with these categories will remain unchanged
        # we set their bias to a large negative value to prevent their outputs
        if len(empty_cls) > 0:
            bias_value = -(math.log((1 - 1e-6) / 1e-6))
            for idx in empty_cls:
                torch.nn.init.constant_(self.cls_head.conv.bias[idx], bias_value)

    def forward(self, fpn_feats, fpn_masks):
        assert len(fpn_feats) == len(fpn_masks)

        # apply the classifier for each pyramid level
        out_logits = tuple()
        for _, (cur_feat, cur_mask) in enumerate(zip(fpn_feats, fpn_masks)):
            if self.detach_feat:
                cur_out = cur_feat.detach()
            else:
                cur_out = cur_feat
            for idx in range(len(self.head)):
                cur_out, _ = self.head[idx](cur_out, cur_mask)
                cur_out = self.act(self.norm[idx](cur_out))
            cur_logits, _ = self.cls_head(cur_out, cur_mask)
            out_logits += (cur_logits,)

            # fpn_masks remains the same
        return out_logits


class PtTransformerRegHead(nn.Module):
    """
    Shared 1D Conv heads for regression
    Simlar logic as PtTransformerClsHead with separated implementation for clarity
    """
    def __init__(
        self,
        input_dim,
        feat_dim,
        fpn_levels,
        num_layers=3,
        kernel_size=3,
        act_layer=nn.ReLU,
        with_ln=False,
        num_bins = 16
    ):
        super().__init__()
        self.fpn_levels = fpn_levels
        self.act = act_layer()

        # build the conv head
        self.head = nn.ModuleList()
        self.norm = nn.ModuleList()
        for idx in range(num_layers-1):
            if idx == 0:
                in_dim = input_dim
                out_dim = feat_dim
            else:
                in_dim = feat_dim
                out_dim = feat_dim
            self.head.append(
                MaskedConv1D(
                    in_dim, out_dim, kernel_size,
                    stride=1,
                    padding=kernel_size//2,
                    bias=(not with_ln)
                )
            )
            if with_ln:
                self.norm.append(LayerNorm(out_dim))
            else:
                self.norm.append(nn.Identity())

        self.scale = nn.ModuleList()
        for idx in range(fpn_levels):
            self.scale.append(Scale())

        # segment regression
        self.offset_head = MaskedConv1D(
            feat_dim, 2 * (num_bins + 1), kernel_size,
            stride=1, padding=kernel_size // 2
        )

    def forward(self, fpn_feats, fpn_masks):
        assert len(fpn_feats) == len(fpn_masks)
        assert len(fpn_feats) == self.fpn_levels

        # apply the classifier for each pyramid level
        out_offsets = tuple()
        for l, (cur_feat, cur_mask) in enumerate(zip(fpn_feats, fpn_masks)):
            cur_out = cur_feat
            for idx in range(len(self.head)):
                cur_out, _ = self.head[idx](cur_out, cur_mask)
                cur_out = self.act(self.norm[idx](cur_out))
            cur_offsets, _ = self.offset_head(cur_out, cur_mask)
            out_offsets += (F.relu(self.scale[l](cur_offsets)), )

        # fpn_masks remains the same
        return out_offsets


@register_meta_arch("LocPointTransformer")
class PtTransformer(nn.Module):
    """
        Transformer based model for single stage action localization
    """
    def __init__(
        self,
        backbone_type,         # a string defines which backbone we use
        fpn_type,              # a string defines which fpn we use
        backbone_arch,         # a tuple defines #layers in embed / stem / branch
        scale_factor,          # scale factor between branch layers
        input_dim,             # input feat dim
        max_seq_len,           # max sequence length (used for training)
        max_buffer_len_factor, # max buffer size (defined a factor of max_seq_len)
        n_head,                # number of heads for self-attention in transformer
        n_mha_win_size,        # window size for self attention; -1 to use full seq
        embd_kernel_size,      # kernel size of the embedding network
        embd_dim,              # output feat channel of the embedding network
        embd_with_ln,          # attach layernorm to embedding network
        fpn_dim,               # feature dim on FPN
        fpn_with_ln,           # if to apply layer norm at the end of fpn
        fpn_start_level,       # start level of fpn
        head_dim,              # feature dim for head
        regression_range,      # regression range on each level of FPN
        head_num_layers,       # number of layers in the head (including the classifier)
        head_kernel_size,      # kernel size for reg/cls heads
        head_with_ln,          # attache layernorm to reg/cls heads
        use_abs_pe,            # if to use abs position encoding
        use_rel_pe,            # if to use rel position encoding
        num_classes,           # number of action classes
        train_cfg,             # other cfg for training
        test_cfg               # other cfg for testing
    ):
        super().__init__()
         # re-distribute params to backbone / neck / head
        use_trident_head = True
        self.fpn_strides = [scale_factor**i for i in range(
            fpn_start_level, backbone_arch[-1]+1
        )]
        self.reg_range = regression_range
        assert len(self.fpn_strides) == len(self.reg_range)
        self.scale_factor = scale_factor
        # #classes = num_classes + 1 (background) with last category as background
        # e.g., num_classes = 10 -> 0, 1, ..., 9 as actions, 10 as background
        self.num_classes = num_classes
        embd_dim1 = 512
        self.fc = nn.Linear(embd_dim1, embd_dim1)
        self.activation = nn.ReLU()
        self.fc_feats = nn.Linear(2048, 2048)
        self.fc_clip_feats = nn.Linear(512, 512)
       # self.fc_text_feats = nn.Linear(10240, 10240)
       # self.layer_norm = nn.LayerNorm(embd_dim1)
        # check the feature pyramid and local attention window size
        self.max_seq_len = max_seq_len
        if isinstance(n_mha_win_size, int):
            self.mha_win_size = [n_mha_win_size]*(1 + backbone_arch[-1])
        else:
            assert len(n_mha_win_size) == (1 + backbone_arch[-1])
            self.mha_win_size = n_mha_win_size
        max_div_factor = 1
        for l, (s, w) in enumerate(zip(self.fpn_strides, self.mha_win_size)):
            stride = s * (w // 2) * 2 if w > 1 else s
            #print("stride:",stride)
            assert max_seq_len % stride == 0, "max_seq_len must be divisible by fpn stride and window size"
            if max_div_factor < stride:
                max_div_factor = stride
        self.max_div_factor = max_div_factor
        num_bins = 16
        self.num_bins = num_bins
        iou_weight_power = 1.
        self.iou_weight_power = iou_weight_power
        # training time config
        self.train_center_sample = train_cfg['center_sample']
        assert self.train_center_sample in ['radius', 'none']
        self.train_center_sample_radius = train_cfg['center_sample_radius']
        self.train_loss_weight = train_cfg['loss_weight']
        self.train_cls_prior_prob = train_cfg['cls_prior_prob']
        self.train_dropout = train_cfg['dropout']
        self.train_droppath = train_cfg['droppath']
        self.train_label_smoothing = train_cfg['label_smoothing']

        # test time config
        self.test_pre_nms_thresh = test_cfg['pre_nms_thresh']
        self.test_pre_nms_topk = test_cfg['pre_nms_topk']
        self.test_iou_threshold = test_cfg['iou_threshold']
        self.test_min_score = test_cfg['min_score']
        self.test_max_seg_num = test_cfg['max_seg_num']
        self.test_nms_method = test_cfg['nms_method']
        assert self.test_nms_method in ['soft', 'hard', 'none']
        self.test_duration_thresh = test_cfg['duration_thresh']
        self.test_multiclass_nms = test_cfg['multiclass_nms']
        self.test_nms_sigma = test_cfg['nms_sigma']
        self.test_voting_thresh = test_cfg['voting_thresh']

        # we will need a better way to dispatch the params to backbones / necks
        # backbone network: conv + transformer
        assert backbone_type in ['convTransformer', 'conv']
        if backbone_type == 'convTransformer':
            self.backbone = make_backbone(
                'convTransformer',
                **{
                    'n_in' : input_dim,
                    'n_embd' : embd_dim,
                    'n_head': n_head,
                    'n_embd_ks': embd_kernel_size,
                    'max_len': max_seq_len,
                    'arch' : backbone_arch,
                    'mha_win_size': self.mha_win_size,
                    'scale_factor' : scale_factor,
                    'with_ln' : embd_with_ln,
                    'attn_pdrop' : 0.0,
                    'proj_pdrop' : self.train_dropout,
                    'path_pdrop' : self.train_droppath,
                    'use_abs_pe' : use_abs_pe,
                    'use_rel_pe' : use_rel_pe
                }
            )
        else:
            self.backbone = make_backbone(
                'conv',
                **{
                    'n_in': input_dim,
                    'n_embd': embd_dim,
                    'n_embd_ks': embd_kernel_size,
                    'arch': backbone_arch,
                    'scale_factor': scale_factor,
                    'with_ln' : embd_with_ln
                }
            )
        if isinstance(embd_dim, (list, tuple)):
            embd_dim = sum(embd_dim)

        # fpn network: convs
        assert fpn_type in ['fpn', 'identity']
        self.neck = make_neck(
            fpn_type,
            **{
                'in_channels' : [embd_dim] * (backbone_arch[-1] + 1),
                'out_channel' : fpn_dim,
                'scale_factor' : scale_factor,
                'start_level' : fpn_start_level,
                'with_ln' : fpn_with_ln
            }
        )
      #  alpha = [0.70] * 20 + [0.01]
       # self.focal_loss = MultiCEFocalLoss(alpha=alpha, gamma=2, num_classes=21, size_average=False)
        # location generator: points
        self.point_generator = make_generator(
            'point',
            **{
                'max_seq_len' : max_seq_len * max_buffer_len_factor,
                'fpn_strides' : self.fpn_strides,
                'regression_range' : self.reg_range
            }
        )
        # classfication and regerssion heads
        self.cls_head = PtTransformerClsHead(
            fpn_dim, head_dim, self.num_classes,
            kernel_size=head_kernel_size,
            prior_prob=self.train_cls_prior_prob,
            with_ln=head_with_ln,
            num_layers=head_num_layers,
            empty_cls=train_cfg['head_empty_cls']
        )
        boudary_kernel_size = 3
        if use_trident_head:
            self.start_head = PtTransformerClsHead(
                fpn_dim, head_dim, self.num_classes,
                kernel_size=boudary_kernel_size,
                prior_prob=self.train_cls_prior_prob,
                with_ln=head_with_ln,
                num_layers=head_num_layers,
                empty_cls=train_cfg['head_empty_cls'],
                detach_feat=True
            )
            self.end_head = PtTransformerClsHead(
                fpn_dim, head_dim, self.num_classes,
                kernel_size=boudary_kernel_size,
                prior_prob=self.train_cls_prior_prob,
                with_ln=head_with_ln,
                num_layers=head_num_layers,
                empty_cls=train_cfg['head_empty_cls'],
                detach_feat=True
            )
            self.use_trident_head = True
            self.reg_head = PtTransformerRegHead(
                fpn_dim, head_dim, len(self.fpn_strides),
                kernel_size=head_kernel_size,
                num_layers=head_num_layers,
                with_ln=head_with_ln,
                num_bins=num_bins
            )
        else:
            self.reg_head = PtTransformerRegHead(
                fpn_dim, head_dim, len(self.fpn_strides),
                kernel_size=head_kernel_size,
                num_layers=head_num_layers,
                with_ln=head_with_ln,
                num_bins=0
            )
        #self.layer_norm = LayerNorm(num_channels=512)
        # maintain an EMA of #foreground to stabilize the loss normalizer
        # useful for small mini-batch training
        self.loss_normalizer = train_cfg['init_loss_norm']
        self.loss_normalizer_momentum = 0.9
        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.08))
    @property
    def device(self):
        # a hacky way to get the device type
        # will throw an error if parameters are on different devices
        return list(set(p.device for p in self.parameters()))[0]
    def decode_offset(self, out_offsets, pred_start_neighbours, pred_end_neighbours):
        # decode the offset value from the network output
        # If a normal regression head is used, the offsets is predicted directly in the out_offsets.
        # If the Trident-head is used, the predicted offset is calculated using the value from
        # center offset head (out_offsets), start boundary head (pred_left) and end boundary head (pred_right)

        if not self.use_trident_head:
            if self.training:
                out_offsets = torch.cat(out_offsets, dim=1)
            return out_offsets

        else:
            # Make an adaption for train and validation, when training, the out_offsets is a list with feature outputs
            # from each FPN level. Each feature with shape [batchsize, T_level, (Num_bin+1)x2].
            # For validation, the out_offsets is a feature with shape [T_level, (Num_bin+1)x2]
            if self.training:
                out_offsets = torch.cat(out_offsets, dim=1)
                out_offsets = out_offsets.view(out_offsets.shape[:2] + (2, -1))
                pred_start_neighbours = torch.cat(pred_start_neighbours, dim=1)
                pred_end_neighbours = torch.cat(pred_end_neighbours, dim=1)

                pred_left_dis = torch.softmax(pred_start_neighbours + out_offsets[:, :, :1, :], dim=-1)
                pred_right_dis = torch.softmax(pred_end_neighbours + out_offsets[:, :, 1:, :], dim=-1)

            else:
                out_offsets = out_offsets.view(out_offsets.shape[0], 2, -1)
                pred_left_dis = torch.softmax(pred_start_neighbours + out_offsets[None, :, 0, :], dim=-1)
                pred_right_dis = torch.softmax(pred_end_neighbours + out_offsets[None, :, 1, :], dim=-1)

            max_range_num = pred_left_dis.shape[-1]

            left_range_idx = torch.arange(max_range_num - 1, -1, -1, device=pred_start_neighbours.device,
                                          dtype=torch.float).unsqueeze(-1)
            right_range_idx = torch.arange(max_range_num, device=pred_end_neighbours.device,
                                           dtype=torch.float).unsqueeze(-1)

            pred_left_dis = pred_left_dis.masked_fill(torch.isnan(pred_right_dis), 0)
            pred_right_dis = pred_right_dis.masked_fill(torch.isnan(pred_right_dis), 0)

            # calculate the value of expectation for the offset:
            decoded_offset_left = torch.matmul(pred_left_dis, left_range_idx)
            decoded_offset_right = torch.matmul(pred_right_dis, right_range_idx)
            return torch.cat([decoded_offset_left, decoded_offset_right], dim=-1)

    def forward(self, video_list):
        # batch the video list into feats (B, C, T) and masks (B, 1, T)

        for video in video_list:
            # 提取每个视频的特征
            feats = video['feats'].transpose(0, 1)
            clip_feats = video['clip_feats'].transpose(0, 1)
            # 映射到相同的空间并应用ReLU激活函数和残差连接
            feats_mapped = F.relu(self.fc_feats(feats)+ feats)
            clip_feats_mapped = F.relu(self.fc_clip_feats(clip_feats)+ clip_feats)
            feats_mapped = feats_mapped.transpose(0, 1)
            clip_feats_mapped = clip_feats_mapped.transpose(0, 1)
            # 拼接特征
            concatenated_feats = torch.cat((feats_mapped, clip_feats_mapped), dim=0)
            # 将拼接后的特征添加到视频字典中
            video['concatenated_feats'] = concatenated_feats

        batched_inputs, batched_masks = self.preprocessing(video_list)
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        text_dir = '/mnt/DataDrive164/caoqiushi/thumos14/text（action）/text_features.npy' #thumos14的文本特征
        #text_dir = '/mnt/DataDrive164/caoqiushi/anet_1.3/text_anet_features.npy'  #anet的文本特征
        text_features = np.load(text_dir)
        text_features = torch.from_numpy(text_features).to(device).float()
        text_features_transformed = self.fc(text_features)
        text_features_transformed = self.activation(text_features_transformed)
        text_features = text_features + text_features_transformed
        text_features = text_features / text_features.norm(dim=1, keepdim=True)
        text_features_expanded = text_features.unsqueeze(0)
        np.set_printoptions(threshold=np.inf)
        # forward the network (backbone -> neck -> heads)
        feats, masks = self.backbone(batched_inputs, batched_masks)
        fpn_feats, fpn_masks = self.neck(feats, masks)
        points = self.point_generator(fpn_feats)
        # 对 fpn_feats 中的每个张量进行处理
        text_norm = text_features_expanded.transpose(1, 2)

        # 对每个层级的特征进行点乘操作
        out_cls_logits = self.cls_head(fpn_feats, fpn_masks)
        # out_offset: List[B, 2, T_i]
        if self.use_trident_head:
            out_lb_logits = self.start_head(fpn_feats, fpn_masks)
            out_rb_logits = self.end_head(fpn_feats, fpn_masks)
        else:
            out_lb_logits = None
            out_rb_logits = None

        out_offsets = self.reg_head(fpn_feats, fpn_masks)
        fpn_feats_transposed = [feat.transpose(1, 2) for feat in fpn_feats]
        clip_cls_logits = tuple()  # 使用元组而不是列表
        for feat, mask in zip(fpn_feats_transposed, fpn_masks):
            #cur_feat = feat.float()
            epsilon = 1e-10 #用于解决分母为0的问题
            logit_scale = self.logit_scale.exp()
            norm = feat.norm(dim=2, keepdim=True) + epsilon
            feat_norm = feat / norm
            cur_clip_cls_logits = logit_scale * feat_norm @ text_norm
            cur_clip_cls_logits = cur_clip_cls_logits.permute(0, 2, 1)

            # 应用 mask
            out_mask = mask.to(feat.dtype)
            cur_clip_cls_logits = cur_clip_cls_logits * out_mask.detach()
            # 使用元组连接
            clip_cls_logits += (cur_clip_cls_logits,)

        # permute the outputs
        # out_cls: F List[B, #cls, T_i] -> F List[B, T_i, #cls]
        clip_cls_logits = [x.permute(0, 2, 1) for x in clip_cls_logits]
        out_cls_logits = [x.permute(0, 2, 1) for x in out_cls_logits]
        # out_offset: F List[B, 2 (xC), T_i] -> F List[B, T_i, 2 (xC)]
        out_offsets = [x.permute(0, 2, 1) for x in out_offsets]
        # fpn_masks: F list[B, 1, T_i] -> F List[B, T_i]
        fpn_masks = [x.squeeze(1) for x in fpn_masks]

        # return loss during training
        if self.training:
            # generate segment/label List[N x 2] / List[N] with length = B
            assert video_list[0]['segments'] is not None, "GT action labels does not exist"
            assert video_list[0]['labels'] is not None, "GT action labels does not exist"
            gt_segments = [x['segments'].to(self.device) for x in video_list]
            gt_labels = [x['labels'].to(self.device) for x in video_list]

            # compute the gt labels for cls & reg
            # list of prediction targets
            gt_cls_labels, gt_offsets, gt_cls2_labels = self.label_points(
                points, gt_segments, gt_labels)

            # compute the loss and return
            losses = self.losses(gt_cls2_labels,
                clip_cls_logits,
                fpn_masks,
                out_cls_logits,
                out_offsets,
                gt_cls_labels, gt_offsets,
                out_lb_logits,out_rb_logits,
            )
            return losses

        else:
            # decode the actions (sigmoid / stride, etc)
            # compute the gt labels for cls & reg
            # list of prediction targets
            results = self.inference(
                out_cls_logits, clip_cls_logits,
                video_list, points, fpn_masks,
                 out_offsets,out_lb_logits, out_rb_logits,
            )
            return results

    @torch.no_grad()
    def preprocessing(self, video_list, padding_val=0.0):
        """
            Generate batched features and masks from a list of dict items
        """
        feats = [x['concatenated_feats'] for x in video_list]
        feats_lens = torch.as_tensor([feat.shape[-1] for feat in feats])
        max_len = feats_lens.max(0).values.item()

        if self.training:
            assert max_len <= self.max_seq_len, "Input length must be smaller than max_seq_len during training"
            # set max_len to self.max_seq_len
            max_len = self.max_seq_len
            # batch input shape B, C, T
            batch_shape = [len(feats), feats[0].shape[0], max_len]
            batched_inputs = feats[0].new_full(batch_shape, padding_val)
            for feat, pad_feat in zip(feats, batched_inputs):
                pad_feat[..., :feat.shape[-1]].copy_(feat)
        else:
            assert len(video_list) == 1, "Only support batch_size = 1 during inference"
            # input length < self.max_seq_len, pad to max_seq_len
            if max_len <= self.max_seq_len:
                max_len = self.max_seq_len
            else:
                # pad the input to the next divisible size
                stride = self.max_div_factor
                max_len = (max_len + (stride - 1)) // stride * stride
            padding_size = [0, max_len - feats_lens[0]]
            batched_inputs = F.pad(
                feats[0], padding_size, value=padding_val).unsqueeze(0)

        # generate the mask
        batched_masks = torch.arange(max_len)[None, :] < feats_lens[:, None]

        # push to device
        batched_inputs = batched_inputs.to(self.device)
        batched_masks = batched_masks.unsqueeze(1).to(self.device)

        return batched_inputs, batched_masks

    @torch.no_grad()
    def label_points(self, points, gt_segments, gt_labels):
        # concat points on all fpn levels List[T x 4] -> F T x 4
        # This is shared for all samples in the mini-batch
        num_levels = len(points)
        concat_points = torch.cat(points, dim=0)
        gt_cls, gt_offset, gt_cls2 = [], [], []

        # loop over each video sample
        for gt_segment, gt_label in zip(gt_segments, gt_labels):
            cls_targets, reg_targets, clip_targets = self.label_points_single_video(
                concat_points, gt_segment, gt_label
            )
            # append to list (len = # images, each of size FT x C)
            gt_cls.append(cls_targets)
            gt_cls2.append(clip_targets)
            gt_offset.append(reg_targets)

        return gt_cls, gt_offset, gt_cls2

    @torch.no_grad()
    def label_points_single_video(self, concat_points, gt_segment, gt_label):
        # concat_points : F T x 4 (t, regression range, stride)
        # gt_segment : N (#Events) x 2
        # gt_label : N (#Events) x 1
        num_pts = concat_points.shape[0]
        num_gts = gt_segment.shape[0]

        # corner case where current sample does not have actions
        if num_gts == 0:
            cls_targets = gt_segment.new_full((num_pts, self.num_classes), 0)
            clip_targets = gt_segment.new_full((num_pts, 200), 0)
            reg_targets = gt_segment.new_zeros((num_pts, 2))
            return cls_targets, reg_targets, clip_targets

        # compute the lengths of all segments -> F T x N
        lens = gt_segment[:, 1] - gt_segment[:, 0]
        lens = lens[None, :].repeat(num_pts, 1)

        # compute the distance of every point to each segment boundary
        # auto broadcasting for all reg target-> F T x N x2
        gt_segs = gt_segment[None].expand(num_pts, num_gts, 2)
        left = concat_points[:, 0, None] - gt_segs[:, :, 0]
        right = gt_segs[:, :, 1] - concat_points[:, 0, None]
        reg_targets = torch.stack((left, right), dim=-1)

        if self.train_center_sample == 'radius':
            # center of all segments F T x N
            center_pts = 0.5 * (gt_segs[:, :, 0] + gt_segs[:, :, 1])
            # center sampling based on stride radius
            # compute the new boundaries:
            # concat_points[:, 3] stores the stride
            t_mins = \
                center_pts - concat_points[:, 3, None] * self.train_center_sample_radius
            t_maxs = \
                center_pts + concat_points[:, 3, None] * self.train_center_sample_radius
            # prevent t_mins / maxs from over-running the action boundary
            # left: torch.maximum(t_mins, gt_segs[:, :, 0])
            # right: torch.minimum(t_maxs, gt_segs[:, :, 1])
            # F T x N (distance to the new boundary)
            cb_dist_left = concat_points[:, 0, None] \
                           - torch.maximum(t_mins, gt_segs[:, :, 0])
            cb_dist_right = torch.minimum(t_maxs, gt_segs[:, :, 1]) \
                            - concat_points[:, 0, None]
            # F T x N x 2
            center_seg = torch.stack(
                (cb_dist_left, cb_dist_right), -1)
            # F T x N
            inside_gt_seg_mask = center_seg.min(-1)[0] > 0
        else:
            # inside an gt action
            inside_gt_seg_mask = reg_targets.min(-1)[0] > 0

        # limit the regression range for each location
        max_regress_distance = reg_targets.max(-1)[0]
        # F T x N
        inside_regress_range = torch.logical_and(
            (max_regress_distance >= concat_points[:, 1, None]),
            (max_regress_distance <= concat_points[:, 2, None])
        )

        # if there are still more than one actions for one moment
        # pick the one with the shortest duration (easiest to regress)
        lens.masked_fill_(inside_gt_seg_mask==0, float('inf'))
        lens.masked_fill_(inside_regress_range==0, float('inf'))
        # F T x N -> F T
        min_len, min_len_inds = lens.min(dim=1)

        # corner case: multiple actions with very similar durations (e.g., THUMOS14)
        min_len_mask = torch.logical_and(
            (lens <= (min_len[:, None] + 1e-3)), (lens < float('inf'))
        ).to(reg_targets.dtype)

        # cls_targets: F T x C; reg_targets F T x 2
        gt_label_one_hot = F.one_hot(
            gt_label, self.num_classes
        ).to(reg_targets.dtype)

        gt_label_one_hot2 = F.one_hot(
            gt_label, 200
        ).to(reg_targets.dtype)

        cls_targets = min_len_mask @ gt_label_one_hot
        clip_targets = min_len_mask @ gt_label_one_hot2
        # to prevent multiple GT actions with the same label and boundaries
        clip_targets.clamp_(min=0.0, max=1.0)
        cls_targets.clamp_(min=0.0, max=1.0)
        # OK to use min_len_inds
        reg_targets = reg_targets[range(num_pts), min_len_inds]
        # normalization based on stride
        reg_targets /= concat_points[:, 3, None]

        return cls_targets, reg_targets, clip_targets

    def losses(
        self, gt_cls2_labels, clip_cls_logits,
             fpn_masks,
        out_cls_logits,
            out_offsets,
        gt_cls_labels, gt_offsets,
        out_start, out_end,
    ):
        # fpn_masks, out_*: F (List) [B, T_i, C]
        # gt_* : B (list) [F T, C]
        # fpn_masks -> (B, FT)
        valid_mask = torch.cat(fpn_masks, dim=1)
        if self.use_trident_head:
            out_start_logits = []
            out_end_logits = []
            for i in range(len(out_start)):
                x = (F.pad(out_start[i], (self.num_bins, 0), mode='constant', value=0)).unsqueeze(-1)  # pad left
                x_size = list(x.size())  # bz, cls_num, T+num_bins, 1
                x_size[-1] = self.num_bins + 1  # bz, cls_num, T+num_bins, num_bins + 1
                x_size[-2] = x_size[-2] - self.num_bins  # bz, cls_num, T+num_bins, num_bins + 1
                x_stride = list(x.stride())
                x_stride[-2] = x_stride[-1]
                x = x.as_strided(size=x_size, stride=x_stride)
                out_start_logits.append(x.permute(0, 2, 1, 3))

                x = (F.pad(out_end[i], (0, self.num_bins), mode='constant', value=0)).unsqueeze(-1)  # pad right
                x = x.as_strided(size=x_size, stride=x_stride)
                out_end_logits.append(x.permute(0, 2, 1, 3))
        else:
            out_start_logits = None
            out_end_logits = None
        # 1. classification loss
        # stack the list -> (B, FT) -> (# Valid, )
        gt_cls = torch.stack(gt_cls_labels)
        clip_cls = torch.stack(gt_cls2_labels)

        pos_mask = torch.logical_and((gt_cls.sum(-1) > 0), valid_mask)

        decoded_offsets = self.decode_offset(out_offsets, out_start_logits, out_end_logits)  # bz, stack_T, num_class, 2
        decoded_offsets = decoded_offsets[pos_mask]

        # cat the predicted offsets -> (B, FT, 2 (xC)) -> # (#Pos, 2 (xC))
        if self.use_trident_head:
            # the boundary head predicts the classification score for each categories.
            pred_offsets = decoded_offsets[gt_cls[pos_mask].bool()]
            # cat the predicted offsets -> (B, FT, 2 (xC)) -> # (#Pos, 2 (xC))
            vid = torch.where(gt_cls[pos_mask])[0]
            gt_offsets = torch.stack(gt_offsets)[pos_mask][vid]
        else:
            pred_offsets = decoded_offsets
            gt_offsets = torch.stack(gt_offsets)[pos_mask]

        # update the loss normalizer
        num_pos = pos_mask.sum().item()
        self.loss_normalizer = self.loss_normalizer_momentum * self.loss_normalizer + (
            1 - self.loss_normalizer_momentum
        ) * max(num_pos, 1)

        # gt_cls is already one hot encoded now, simply masking out
        gt_target = gt_cls[valid_mask]
        clip_target = clip_cls[valid_mask]
        # optinal label smoothing
        clip_target *= 1 - self.train_label_smoothing
        clip_target += self.train_label_smoothing / (200 + 1)

        gt_target *= 1 - self.train_label_smoothing
        gt_target += self.train_label_smoothing / (self.num_classes + 1)
        #with open('gt_target.txt', 'a') as f:
         #   f.write(str(gt_target))

        # focal loss
        cls_loss = sigmoid_focal_loss(
            torch.cat(out_cls_logits, dim=1)[valid_mask],
            gt_target,
            reduction='none'
        )

        if self.use_trident_head:
            # couple the classification loss with iou score
            iou_rate = ctr_giou_loss_1d(
                pred_offsets,
                gt_offsets,
                reduction='none'
            )
            rated_mask = gt_target > self.train_label_smoothing / (self.num_classes + 1)
            cls_loss[rated_mask] *= (1 - iou_rate) ** self.iou_weight_power

        cls_loss = cls_loss.sum()
        cls_loss /= self.loss_normalizer

        #if train in Thumos14 please use gt_target as clip_target,if train in Anet please use clip_target
        clip_loss = sigmoid_focal_loss(
            torch.cat(clip_cls_logits, dim=1)[valid_mask],
            clip_target,
            reduction='sum'
        )
        clip_loss /= self.loss_normalizer
        # 2. regression using IoU/GIoU loss (defined on positive samples)
        if num_pos == 0:
            reg_loss = 0 * pred_offsets.sum()
        else:
            # giou loss defined on positive samples
            reg_loss = ctr_diou_loss_1d(
                pred_offsets,
                gt_offsets,
                reduction='sum'
            )
            reg_loss /= self.loss_normalizer
        if self.train_loss_weight > 0:
            loss_weight = self.train_loss_weight
        else:
            loss_weight = cls_loss.detach() / max(reg_loss.item(), 0.01)
        alpha = 0.3
        beta = 0.4
        final_loss = alpha * cls_loss + clip_loss * beta + reg_loss * loss_weight * (1 - alpha - beta)
        return {'cls_loss'   : cls_loss,
                'reg_loss'   : reg_loss,
                'clip_loss' : clip_loss,
                'final_loss' : final_loss
               }

    @torch.no_grad()
    def inference(
        self,out_cls_logits, clip_cls_logits,
        video_list,
        points, fpn_masks,
         out_offsets,
        out_lb_logits, out_rb_logits,
    ):
        # video_list B (list) [dict]
        # points F (list) [T_i, 4]
        # fpn_masks, out_*: F (List) [B, T_i, C]
        results = []
        # 1: gather video meta information
        vid_idxs = [x['video_id'] for x in video_list]
        vid_fps = [x['fps'] for x in video_list]
        vid_lens = [x['duration'] for x in video_list]
        vid_ft_stride = [x['feat_stride'] for x in video_list]
        vid_ft_nframes = [x['feat_num_frames'] for x in video_list]

        # 2: inference on each single video and gather the results
        # upto this point, all results use timestamps defined on feature grids
        for idx, (vidx, fps, vlen, stride, nframes) in enumerate(
            zip(vid_idxs, vid_fps, vid_lens, vid_ft_stride, vid_ft_nframes)
        ):
            # gather per-video outputs
            cls_logits_per_vid = [x[idx] for x in out_cls_logits]
            clip_logits_per_vid = [x[idx] for x in clip_cls_logits]
            offsets_per_vid = [x[idx] for x in out_offsets]
            fpn_masks_per_vid = [x[idx] for x in fpn_masks]

            if self.use_trident_head:
                lb_logits_per_vid = [x[idx] for x in out_lb_logits]
                rb_logits_per_vid = [x[idx] for x in out_rb_logits]
            else:
                lb_logits_per_vid = [None for x in range(len(out_cls_logits))]
                rb_logits_per_vid = [None for x in range(len(out_cls_logits))]
            # inference on a single video (should always be the case)
            results_per_vid = self.inference_single_video(
                vidx,
                cls_logits_per_vid,
                clip_logits_per_vid,
                points, fpn_masks_per_vid,
                offsets_per_vid,
                lb_logits_per_vid, rb_logits_per_vid
            )
            # pass through video meta info
            results_per_vid['video_id'] = vidx
            results_per_vid['fps'] = fps
            results_per_vid['duration'] = vlen
            results_per_vid['feat_stride'] = stride
            results_per_vid['feat_num_frames'] = nframes
            results.append(results_per_vid)

        # step 3: postprocssing
        results = self.postprocessing(results)

        return results

    @torch.no_grad()
    def inference_single_video(
        self,
        vidx,
        cls_logits_per_vid,
        clip_logits_per_vid,
        points,
        fpn_masks,
        out_offsets,
        lb_logits_per_vid, rb_logits_per_vid
    ):
        # points F (list) [T_i, 4]
        # fpn_masks, out_*: F (List) [T_i, C]
        segs_all = []
        scores_all = []
        cls_idxs_all = []
        # loop over fpn levels
        alpha = 0.7
        beta = 0.3
        for cls_i, clip_i , offsets_i, pts_i, mask_i,sb_cls_i, eb_cls_i in zip(cls_logits_per_vid, clip_logits_per_vid,
              out_offsets, points, fpn_masks,lb_logits_per_vid, rb_logits_per_vid
            ):
           # if inference in Thumos14 combined_prob：
           # combined_prob = alpha * cls_i.sigmoid() + beta * clip_i.sigmoid()
           # if inference in Anet combined_prob：
            clip_j, _ = torch.max(clip_i, dim=1, keepdim=True)
            combined_prob = alpha * cls_i.sigmoid() + beta * clip_j.sigmoid()

            pred_prob = (combined_prob * mask_i.unsqueeze(-1)).flatten()

            # Apply filtering to make NMS faster following detectron2
            # 1. Keep seg with confidence score > a threshold
            keep_idxs1 = (pred_prob > self.test_pre_nms_thresh)
            pred_prob = pred_prob[keep_idxs1]
            topk_idxs = keep_idxs1.nonzero(as_tuple=True)[0]

            # 2. Keep top k top scoring boxes only
            num_topk = min(self.test_pre_nms_topk, topk_idxs.size(0))
            pred_prob, idxs = pred_prob.sort(descending=True)
            pred_prob = pred_prob[:num_topk].clone()
            topk_idxs = topk_idxs[idxs[:num_topk]].clone()

            # fix a warning in pytorch 1.9
            pt_idxs =  torch.div(
                topk_idxs, self.num_classes, rounding_mode='floor'
            )
            cls_idxs = torch.fmod(topk_idxs, self.num_classes)
            if self.use_trident_head:
                # pad the boarder
                x = (F.pad(sb_cls_i, (self.num_bins, 0), mode='constant', value=0)).unsqueeze(-1)  # pad left
                x_size = list(x.size())  # cls_num, T+num_bins, 1
                x_size[-1] = self.num_bins + 1
                x_size[-2] = x_size[-2] - self.num_bins  # cls_num, T, num_bins + 1
                x_stride = list(x.stride())
                x_stride[-2] = x_stride[-1]

                pred_start_neighbours = x.as_strided(size=x_size, stride=x_stride)

                x = (F.pad(eb_cls_i, (0, self.num_bins), mode='constant', value=0)).unsqueeze(-1)  # pad right
                pred_end_neighbours = x.as_strided(size=x_size, stride=x_stride)
            else:
                pred_start_neighbours = None
                pred_end_neighbours = None

            decoded_offsets = self.decode_offset(offsets_i, pred_start_neighbours, pred_end_neighbours)

            # pick topk output from the prediction
            if self.use_trident_head:
                offsets = decoded_offsets[cls_idxs, pt_idxs]
            else:
                offsets = decoded_offsets[pt_idxs]
            # 3. gather predicted offsets

            pts = pts_i[pt_idxs]

            # 4. compute predicted segments (denorm by stride for output offsets)
            seg_left = pts[:, 0] - offsets[:, 0] * pts[:, 3]
            seg_right = pts[:, 0] + offsets[:, 1] * pts[:, 3]
            pred_segs = torch.stack((seg_left, seg_right), -1)
            # 5. Keep seg with duration > a threshold (relative to feature grids)
            seg_areas = seg_right - seg_left
            keep_idxs2 = seg_areas > self.test_duration_thresh

            # *_all : N (filtered # of segments) x 2 / 1
            segs_all.append(pred_segs[keep_idxs2])
            scores_all.append(pred_prob[keep_idxs2])
            cls_idxs_all.append(cls_idxs[keep_idxs2])

        # cat along the FPN levels (F N_i, C)
        segs_all, scores_all, cls_idxs_all = [
            torch.cat(x) for x in [segs_all, scores_all, cls_idxs_all]
        ]
        results = {'segments' : segs_all,
                   'scores'   : scores_all,
                   'labels'   : cls_idxs_all}
        return results

    @torch.no_grad()
    def postprocessing(self, results):
        # input : list of dictionary items
        # (1) push to CPU; (2) NMS; (3) convert to actual time stamps
        processed_results = []
        for results_per_vid in results:
            # unpack the meta info
            vidx = results_per_vid['video_id']
            fps = results_per_vid['fps']
            vlen = results_per_vid['duration']
            stride = results_per_vid['feat_stride']
            nframes = results_per_vid['feat_num_frames']
            # 1: unpack the results and move to CPU
            segs = results_per_vid['segments'].detach().cpu()
            scores = results_per_vid['scores'].detach().cpu()
            labels = results_per_vid['labels'].detach().cpu()
            if self.test_nms_method != 'none':
                # 2: batched nms (only implemented on CPU)
                segs, scores, labels = batched_nms(
                    segs, scores, labels,
                    self.test_iou_threshold,
                    self.test_min_score,
                    self.test_max_seg_num,
                    use_soft_nms = (self.test_nms_method == 'soft'),
                    multiclass = self.test_multiclass_nms,
                    sigma = self.test_nms_sigma,
                    voting_thresh = self.test_voting_thresh
                )
            # 3: convert from feature grids to seconds
            if segs.shape[0] > 0:
                segs = (segs * stride + 0.5 * nframes) / fps
                # truncate all boundaries within [0, duration]
                segs[segs<=0.0] *= 0.0
                segs[segs>=vlen] = segs[segs>=vlen] * 0.0 + vlen
            
            # 4: repack the results
            processed_results.append(
                {'video_id' : vidx,
                 'segments' : segs,
                 'scores'   : scores,
                 'labels'   : labels}
            )

        return processed_results