# Copyright 2020 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import tempfile
import shutil
from glob import glob
import logging
import nibabel as nib
import numpy as np
import torch
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

import monai
from monai.transforms import \
    Compose, LoadNiftid, AsChannelFirstd, Rescaled, RandCropByPosNegLabeld, RandRotate90d, ToTensord
from monai.data import create_test_image_3d, list_data_collate, sliding_window_inference
from monai.metrics import compute_meandice
from monai.visualize import plot_2d_or_3d_image

monai.config.print_config()
logging.basicConfig(stream=sys.stdout, level=logging.INFO)

# create a temporary directory and 40 random image, mask paris
tempdir = tempfile.mkdtemp()
print('generating synthetic data to {} (this may take a while)'.format(tempdir))
for i in range(40):
    im, seg = create_test_image_3d(128, 128, 128, num_seg_classes=1, channel_dim=-1)

    n = nib.Nifti1Image(im, np.eye(4))
    nib.save(n, os.path.join(tempdir, 'img%i.nii.gz' % i))

    n = nib.Nifti1Image(seg, np.eye(4))
    nib.save(n, os.path.join(tempdir, 'seg%i.nii.gz' % i))

images = sorted(glob(os.path.join(tempdir, 'img*.nii.gz')))
segs = sorted(glob(os.path.join(tempdir, 'seg*.nii.gz')))
train_files = [{'img': img, 'seg': seg} for img, seg in zip(images[:20], segs[:20])]
val_files = [{'img': img, 'seg': seg} for img, seg in zip(images[-20:], segs[-20:])]

# define transforms for image and segmentation
train_transforms = Compose([
    LoadNiftid(keys=['img', 'seg']),
    AsChannelFirstd(keys=['img', 'seg'], channel_dim=-1),
    Rescaled(keys=['img', 'seg']),
    RandCropByPosNegLabeld(keys=['img', 'seg'], label_key='seg', size=[96, 96, 96], pos=1, neg=1, num_samples=4),
    RandRotate90d(keys=['img', 'seg'], prob=0.5, spatial_axes=[0, 2]),
    ToTensord(keys=['img', 'seg'])
])
val_transforms = Compose([
    LoadNiftid(keys=['img', 'seg']),
    AsChannelFirstd(keys=['img', 'seg'], channel_dim=-1),
    Rescaled(keys=['img', 'seg']),
    ToTensord(keys=['img', 'seg'])
])

# define dataset, dataloader
check_ds = monai.data.Dataset(data=train_files, transform=train_transforms)
# use batch_size=2 to load images and use RandCropByPosNegLabeld to generate 2 x 4 images for network training
check_loader = DataLoader(check_ds, batch_size=2, num_workers=4, collate_fn=list_data_collate,
                          pin_memory=torch.cuda.is_available())
check_data = monai.utils.misc.first(check_loader)
print(check_data['img'].shape, check_data['seg'].shape)

# create a training data loader
train_ds = monai.data.Dataset(data=train_files, transform=train_transforms)
# use batch_size=2 to load images and use RandCropByPosNegLabeld to generate 2 x 4 images for network training
train_loader = DataLoader(train_ds, batch_size=2, shuffle=True, num_workers=4,
                          collate_fn=list_data_collate, pin_memory=torch.cuda.is_available())
# create a validation data loader
val_ds = monai.data.Dataset(data=val_files, transform=val_transforms)
val_loader = DataLoader(val_ds, batch_size=1, num_workers=4, collate_fn=list_data_collate,
                        pin_memory=torch.cuda.is_available())

# create UNet, DiceLoss and Adam optimizer
device = torch.device("cuda:0")
model = monai.networks.nets.UNet(
    dimensions=3,
    in_channels=1,
    out_channels=1,
    channels=(16, 32, 64, 128, 256),
    strides=(2, 2, 2, 2),
    num_res_units=2,
).to(device)
loss_function = monai.losses.DiceLoss(do_sigmoid=True)
optimizer = torch.optim.Adam(model.parameters(), 1e-3)

# start a typical PyTorch training
val_interval = 2
best_metric = -1
best_metric_epoch = -1
epoch_loss_values = list()
metric_values = list()
writer = SummaryWriter()
for epoch in range(5):
    print('-' * 10)
    print('Epoch {}/{}'.format(epoch + 1, 5))
    model.train()
    epoch_loss = 0
    step = 0
    for batch_data in train_loader:
        step += 1
        inputs, labels = batch_data['img'].to(device), batch_data['seg'].to(device)
        optimizer.zero_grad()
        outputs = model(inputs)
        loss = loss_function(outputs, labels)
        loss.backward()
        optimizer.step()
        epoch_loss += loss.item()
        epoch_len = len(train_ds) // train_loader.batch_size
        print("%d/%d, train_loss:%0.4f" % (step, epoch_len, loss.item()))
        writer.add_scalar('train_loss', loss.item(), epoch_len * epoch + step)
    epoch_loss /= step
    epoch_loss_values.append(epoch_loss)
    print("epoch %d average loss:%0.4f" % (epoch + 1, epoch_loss))

    if (epoch + 1) % val_interval == 0:
        model.eval()
        with torch.no_grad():
            metric_sum = 0.
            metric_count = 0
            val_images = None
            val_labels = None
            val_outputs = None
            for val_data in val_loader:
                val_images, val_labels = val_data['img'].to(device), val_data['seg'].to(device)
                roi_size = (96, 96, 96)
                sw_batch_size = 4
                val_outputs = sliding_window_inference(val_images, roi_size, sw_batch_size, model)
                value = compute_meandice(y_pred=val_outputs, y=val_labels, include_background=True,
                                         to_onehot_y=False, add_sigmoid=True)
                metric_count += len(value)
                metric_sum += value.sum().item()
            metric = metric_sum / metric_count
            metric_values.append(metric)
            if metric > best_metric:
                best_metric = metric
                best_metric_epoch = epoch + 1
                torch.save(model.state_dict(), 'best_metric_model.pth')
                print('saved new best metric model')
            print("current epoch %d current mean dice: %0.4f best mean dice: %0.4f at epoch %d"
                  % (epoch + 1, metric, best_metric, best_metric_epoch))
            writer.add_scalar('val_mean_dice', metric, epoch + 1)
            # plot the last model output as GIF image in TensorBoard with the corresponding image and label
            plot_2d_or_3d_image(val_images, epoch + 1, writer, index=0, tag='image')
            plot_2d_or_3d_image(val_labels, epoch + 1, writer, index=0, tag='label')
            plot_2d_or_3d_image(val_outputs, epoch + 1, writer, index=0, tag='output')
shutil.rmtree(tempdir)
print('train completed, best_metric: %0.4f  at epoch: %d' % (best_metric, best_metric_epoch))
writer.close()