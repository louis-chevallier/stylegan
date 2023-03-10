#!/usr/bin/env python
# coding: utf-8

# # Image Generation using Stylegan pre-trained model
# https://www.kaggle.com/code/lmdm99/image-generation-using-stylegan-pre-trained-model/edit
from utillc import *
import torch, sys
import torch.nn as nn
import torch.nn.functional as F

from collections import OrderedDict
import pickle
from facenet_pytorch import MTCNN, InceptionResnetV1
import numpy as np

import IPython


# ### Step 2. Design Layers

# **2-a. Linear Layer**

# In[2]:


class MyLinear(nn.Module):
    """Linear layer with equalized learning rate and custom learning rate multiplier."""
    def __init__(self, input_size, output_size, gain=2**(0.5), use_wscale=False, lrmul=1, bias=True):
        super().__init__()
        he_std = gain * input_size**(-0.5) # He init
        # Equalized learning rate and custom learning rate multiplier.
        if use_wscale:
            init_std = 1.0 / lrmul
            self.w_mul = he_std * lrmul
        else:
            init_std = he_std / lrmul
            self.w_mul = lrmul
        self.weight = torch.nn.Parameter(torch.randn(output_size, input_size) * init_std)
        if bias:
            self.bias = torch.nn.Parameter(torch.zeros(output_size))
            self.b_mul = lrmul
        else:
            self.bias = None

    def forward(self, x):
        bias = self.bias
        if bias is not None:
            bias = bias * self.b_mul
        return F.linear(x, self.weight * self.w_mul, bias)


# > With this Class, Targeted initialization is performed for each layer. 
# It allows generator to follow the targeted style distribution.

# ![image](https://bloglunit.files.wordpress.com/2019/02/e18489e185b3e1848fe185b3e18485e185b5e186abe18489e185a3e186ba-2019-02-24-e1848be185a9e18492e185ae-5.42.19.png?w=1222)

# > TEST CODE :

# In[3]:


gain = 2**(0.5)
gain


# In[4]:


he_std = gain*(512**(-0.5)) # input_size = 512
he_std


# In[5]:


lrmul = 1
init_std = 1.0/lrmul
print(init_std)
print('w_mul when use wscale :',he_std*init_std)


# In[6]:


torch.randn(512,512)*init_std


# In[7]:


weight = torch.nn.Parameter(torch.randn(512,512)*init_std) # Parameter(..) ==> requires_grad=True
weight


# In[8]:


bias = torch.nn.Parameter(torch.zeros(512,512))
bias


# In[9]:


w_mul = lrmul
b_mul = lrmul

F.linear(torch.randn(512,512), weight*w_mul, bias*b_mul)


# **2-b. Convolution Layer**

# In[10]:


class MyConv2d(nn.Module):
    """Conv layer with equalized learning rate and custom learning rate multiplier."""
    def __init__(self, input_channels, output_channels, kernel_size, gain=2**(0.5), use_wscale=False, lrmul=1, bias=True,
                intermediate=None, upscale=False):
        super().__init__()
        if upscale:
            self.upscale = Upscale2d()
        else:
            self.upscale = None
        he_std = gain * (input_channels * kernel_size ** 2) ** (-0.5) # He init
        self.kernel_size = kernel_size
        if use_wscale:
            init_std = 1.0 / lrmul
            self.w_mul = he_std * lrmul
        else:
            init_std = he_std / lrmul
            self.w_mul = lrmul
        self.weight = torch.nn.Parameter(torch.randn(output_channels, input_channels, kernel_size, kernel_size) * init_std)
        if bias:
            self.bias = torch.nn.Parameter(torch.zeros(output_channels))
            self.b_mul = lrmul
        else:
            self.bias = None
        self.intermediate = intermediate

    def forward(self, x):
        bias = self.bias
        if bias is not None:
            bias = bias * self.b_mul
        
        have_convolution = False
        if self.upscale is not None and min(x.shape[2:]) * 2 >= 128:
            # this is the fused upscale + conv from StyleGAN, sadly this seems incompatible with the non-fused way
            # this really needs to be cleaned up and go into the conv...
            w = self.weight * self.w_mul
            w = w.permute(1, 0, 2, 3)
            # probably applying a conv on w would be more efficient. also this quadruples the weight (average)?!
            w = F.pad(w, (1,1,1,1))
            w = w[:, :, 1:, 1:]+ w[:, :, :-1, 1:] + w[:, :, 1:, :-1] + w[:, :, :-1, :-1]
            x = F.conv_transpose2d(x, w, stride=2, padding=(w.size(-1)-1)//2)
            have_convolution = True
        elif self.upscale is not None:
            x = self.upscale(x)
    
        if not have_convolution and self.intermediate is None:
            return F.conv2d(x, self.weight * self.w_mul, bias, padding=self.kernel_size//2)
        elif not have_convolution:
            x = F.conv2d(x, self.weight * self.w_mul, None, padding=self.kernel_size//2)
        
        if self.intermediate is not None:
            x = self.intermediate(x)
        if bias is not None:
            x = x + bias.view(1, -1, 1, 1)
        return x


# > Using the same metric(targeted initialization)

# Let's look at the schematic again at this point. 

# ![image](https://www.researchgate.net/publication/343021405/figure/fig3/AS:915394470625280@1595258457162/Generator-architecture-of-the-StyleGAN-neural-network-1.png)

# > For each block, 2 noises and 2 styles are continuously injected.

# **2-c. Noise Layer**

# In[11]:


class NoiseLayer(nn.Module):
    """adds noise. noise is per pixel (constant over channels) with per-channel weight"""
    def __init__(self, channels):
        super().__init__()
        self.weight = nn.Parameter(torch.zeros(channels))
        self.noise = None
    
    def forward(self, x, noise=None):
        if noise is None and self.noise is None:
            noise = torch.randn(x.size(0), 1, x.size(2), x.size(3), device=x.device, dtype=x.dtype)
        elif noise is None:
            # here is a little trick: if you get all the noiselayers and set each
            # modules .noise attribute, you can have pre-defined noise.
            # Very useful for analysis
            noise = self.noise
        x = x + self.weight.view(1, -1, 1, 1) * noise
        return x


# ![image](https://bloglunit.files.wordpress.com/2019/02/1_gwchaliormc1xlj7bh0zmg.png)

# - The noise layer receives the channels and returns the channels to which the noise is applied.
# - The noise layer adds gaussian noise of learnable standard deviation 

# **2-d. Style Modification Layer**

# In[12]:


class StyleMod(nn.Module):
    def __init__(self, latent_size, channels, use_wscale):
        super(StyleMod, self).__init__()
        self.lin = MyLinear(latent_size,
                            channels * 2,
                            gain=1.0, use_wscale=use_wscale)
        
    def forward(self, x, latent):
        style = self.lin(latent) # style => [batch_size, n_channels*2]
        shape = [-1, 2, x.size(1)] + (x.dim() - 2) * [1]
        style = style.view(shape)  # [batch_size, 2, n_channels, ...]
        x = x * (style[:, 0] + 1.) + style[:, 1]
        return x


# ![image](https://bloglunit.files.wordpress.com/2019/02/0_uqn4slmhrfykfmjs.png)

# > TEST CODE :

# In[13]:


lin = MyLinear(512, 3*2, 1.0, use_wscale=True)


# In[14]:


latent1 = torch.from_numpy(np.random.randn(3,512).astype(np.float64))
latent2 = torch.from_numpy(np.random.randn(3,512).astype(np.float64))
latent = torch.cat([latent1,latent2], axis=0)


# In[15]:


latent.size()


# **2-e. Pixel Normalization Layer**

# In[16]:


class PixelNormLayer(nn.Module):
    def __init__(self, epsilon=1e-8):
        super().__init__()
        self.epsilon = epsilon
    def forward(self, x):
        return x * torch.rsqrt(torch.mean(x**2, dim=1, keepdim=True) + self.epsilon)


# **2-f. Blur Layer**

# In[17]:


class BlurLayer(nn.Module):
    def __init__(self, kernel=[1, 2, 1], normalize=True, flip=False, stride=1):
        super(BlurLayer, self).__init__()
        kernel=[1, 2, 1]
        kernel = torch.tensor(kernel, dtype=torch.float32)
        kernel = kernel[:, None] * kernel[None, :]
        kernel = kernel[None, None]
        if normalize:
            kernel = kernel / kernel.sum()
        if flip:
            kernel = kernel[:, :, ::-1, ::-1]
        self.register_buffer('kernel', kernel)
        self.stride = stride
    
    def forward(self, x):
        # expand kernel channels
        kernel = self.kernel.expand(x.size(1), -1, -1, -1)
        x = F.conv2d(
            x,
            kernel,
            stride=self.stride,
            padding=int((self.kernel.size(2)-1)/2),
            groups=x.size(1)
        )
        return x


# **2-g. Upscaling Layer**

# In[18]:


def upscale2d(x, factor=2, gain=1):
    assert x.dim() == 4
    if gain != 1:
        x = x * gain
    if factor != 1:
        shape = x.shape
        x = x.view(shape[0], shape[1], shape[2], 1, shape[3], 1).expand(-1, -1, -1, factor, -1, factor)
        x = x.contiguous().view(shape[0], shape[1], factor * shape[2], factor * shape[3])
    return x

class Upscale2d(nn.Module):
    def __init__(self, factor=2, gain=1):
        super().__init__()
        assert isinstance(factor, int) and factor >= 1
        self.gain = gain
        self.factor = factor
    def forward(self, x):
        return upscale2d(x, factor=self.factor, gain=self.gain)


# ### Step 3. Design Networks

# **3-a. Generator Mapping Network**

# In[19]:


class G_mapping(nn.Sequential):
    def __init__(self, nonlinearity='lrelu', use_wscale=True):
        act, gain = {'relu': (torch.relu, np.sqrt(2)),
                     'lrelu': (nn.LeakyReLU(negative_slope=0.2), np.sqrt(2))}[nonlinearity]
        layers = [
            ('pixel_norm', PixelNormLayer()),
            ('dense0', MyLinear(512, 512, gain=gain, lrmul=0.01, use_wscale=use_wscale)),
            ('dense0_act', act),
            ('dense1', MyLinear(512, 512, gain=gain, lrmul=0.01, use_wscale=use_wscale)),
            ('dense1_act', act),
            ('dense2', MyLinear(512, 512, gain=gain, lrmul=0.01, use_wscale=use_wscale)),
            ('dense2_act', act),
            ('dense3', MyLinear(512, 512, gain=gain, lrmul=0.01, use_wscale=use_wscale)),
            ('dense3_act', act),
            ('dense4', MyLinear(512, 512, gain=gain, lrmul=0.01, use_wscale=use_wscale)),
            ('dense4_act', act),
            ('dense5', MyLinear(512, 512, gain=gain, lrmul=0.01, use_wscale=use_wscale)),
            ('dense5_act', act),
            ('dense6', MyLinear(512, 512, gain=gain, lrmul=0.01, use_wscale=use_wscale)),
            ('dense6_act', act),
            ('dense7', MyLinear(512, 512, gain=gain, lrmul=0.01, use_wscale=use_wscale)),
            ('dense7_act', act)
        ]
        super().__init__(OrderedDict(layers))
        
    def forward(self, x):
        x = super().forward(x)
        # Broadcast
        x = x.unsqueeze(1).expand(-1, 18, -1)
        return x


# > sampling latent `z`(gaussian distribution) --> return `w` vector
# 
# > style information is contained in `w`
# 

# In[20]:


class Truncation(nn.Module):
    def __init__(self, avg_latent, max_layer=8, threshold=0.7):
        super().__init__()
        self.max_layer = max_layer
        self.threshold = threshold
        self.register_buffer('avg_latent', avg_latent)
    def forward(self, x):
        assert x.dim() == 3
        interp = torch.lerp(self.avg_latent, x, self.threshold)
        do_trunc = (torch.arange(x.size(1)) < self.max_layer).view(1, -1, 1)
        return torch.where(do_trunc, interp, x)


# **3-b. Generator Synthesis Blocks**

# In[21]:


class LayerEpilogue(nn.Module):
    """Things to do at the end of each layer."""
    def __init__(self, channels, dlatent_size, use_wscale, use_noise, use_pixel_norm, use_instance_norm, use_styles, activation_layer):
        super().__init__()
        layers = []
        if use_noise:
            layers.append(('noise', NoiseLayer(channels)))
        layers.append(('activation', activation_layer))
        if use_pixel_norm:
            layers.append(('pixel_norm', PixelNorm()))
        if use_instance_norm:
            layers.append(('instance_norm', nn.InstanceNorm2d(channels)))
        self.top_epi = nn.Sequential(OrderedDict(layers))
        if use_styles:
            self.style_mod = StyleMod(dlatent_size, channels, use_wscale=use_wscale)
        else:
            self.style_mod = None
    def forward(self, x, dlatents_in_slice=None):
        x = self.top_epi(x)
        if self.style_mod is not None:
            x = self.style_mod(x, dlatents_in_slice)
        else:
            assert dlatents_in_slice is None
        return x


# In[22]:


class InputBlock(nn.Module):
    def __init__(self, nf, dlatent_size, const_input_layer, gain, use_wscale, use_noise, use_pixel_norm, use_instance_norm, use_styles, activation_layer):
        super().__init__()
        self.const_input_layer = const_input_layer
        self.nf = nf
        if self.const_input_layer:
            # called 'const' in tf
            self.const = nn.Parameter(torch.ones(1, nf, 4, 4))
            self.bias = nn.Parameter(torch.ones(nf))
        else:
            self.dense = MyLinear(dlatent_size, nf*16, gain=gain/4, use_wscale=use_wscale) # tweak gain to match the official implementation of Progressing GAN
        self.epi1 = LayerEpilogue(nf, dlatent_size, use_wscale, use_noise, use_pixel_norm, use_instance_norm, use_styles, activation_layer)
        self.conv = MyConv2d(nf, nf, 3, gain=gain, use_wscale=use_wscale)
        self.epi2 = LayerEpilogue(nf, dlatent_size, use_wscale, use_noise, use_pixel_norm, use_instance_norm, use_styles, activation_layer)
        
    def forward(self, dlatents_in_range):
        batch_size = dlatents_in_range.size(0)
        if self.const_input_layer:
            x = self.const.expand(batch_size, -1, -1, -1)
            x = x + self.bias.view(1, -1, 1, 1)
        else:
            x = self.dense(dlatents_in_range[:, 0]).view(batch_size, self.nf, 4, 4)
        x = self.epi1(x, dlatents_in_range[:, 0])
        x = self.conv(x)
        x = self.epi2(x, dlatents_in_range[:, 1])
        return x


# In[23]:


class GSynthesisBlock(nn.Module):
    def __init__(self, in_channels, out_channels, blur_filter, dlatent_size, gain, use_wscale, use_noise, use_pixel_norm, use_instance_norm, use_styles, activation_layer):
        # 2**res x 2**res # res = 3..resolution_log2
        super().__init__()
        if blur_filter:
            blur = BlurLayer(blur_filter)
        else:
            blur = None
        self.conv0_up = MyConv2d(in_channels, out_channels, kernel_size=3, gain=gain, use_wscale=use_wscale,
                                 intermediate=blur, upscale=True)
        self.epi1 = LayerEpilogue(out_channels, dlatent_size, use_wscale, use_noise, use_pixel_norm, use_instance_norm, use_styles, activation_layer)
        self.conv1 = MyConv2d(out_channels, out_channels, kernel_size=3, gain=gain, use_wscale=use_wscale)
        self.epi2 = LayerEpilogue(out_channels, dlatent_size, use_wscale, use_noise, use_pixel_norm, use_instance_norm, use_styles, activation_layer)
            
    def forward(self, x, dlatents_in_range):
        x = self.conv0_up(x)
        x = self.epi1(x, dlatents_in_range[:, 0])
        x = self.conv1(x)
        x = self.epi2(x, dlatents_in_range[:, 1])
        return x


# **3-c. Generator Synthesis Network**

# In[24]:


class G_synthesis(nn.Module):
    def __init__(self,
        dlatent_size        = 512,          # Disentangled latent (W) dimensionality.
        num_channels        = 3,            # Number of output color channels.
        resolution          = 1024,         # Output resolution.
        fmap_base           = 8192,         # Overall multiplier for the number of feature maps.
        fmap_decay          = 1.0,          # log2 feature map reduction when doubling the resolution.
        fmap_max            = 512,          # Maximum number of feature maps in any layer.
        use_styles          = True,         # Enable style inputs?
        const_input_layer   = True,         # First layer is a learned constant?
        use_noise           = True,         # Enable noise inputs?
        randomize_noise     = True,         # True = randomize noise inputs every time (non-deterministic), False = read noise inputs from variables.
        nonlinearity        = 'lrelu',      # Activation function: 'relu', 'lrelu'
        use_wscale          = True,         # Enable equalized learning rate?
        use_pixel_norm      = False,        # Enable pixelwise feature vector normalization?
        use_instance_norm   = True,         # Enable instance normalization?
        dtype               = torch.float32,  # Data type to use for activations and outputs.
        blur_filter         = [1,2,1],      # Low-pass filter to apply when resampling activations. None = no filtering.
        ):
        
        super().__init__()
        def nf(stage):
            return min(int(fmap_base / (2.0 ** (stage * fmap_decay))), fmap_max)
        self.dlatent_size = dlatent_size
        resolution_log2 = int(np.log2(resolution))
        assert resolution == 2**resolution_log2 and resolution >= 4

        act, gain = {'relu': (torch.relu, np.sqrt(2)),
                     'lrelu': (nn.LeakyReLU(negative_slope=0.2), np.sqrt(2))}[nonlinearity]
        num_layers = resolution_log2 * 2 - 2
        num_styles = num_layers if use_styles else 1
        torgbs = []
        blocks = []
        for res in range(2, resolution_log2 + 1):
            channels = nf(res-1)
            name = '{s}x{s}'.format(s=2**res)
            if res == 2:
                blocks.append((name,
                               InputBlock(channels, dlatent_size, const_input_layer, gain, use_wscale,
                                      use_noise, use_pixel_norm, use_instance_norm, use_styles, act)))
                
            else:
                blocks.append((name,
                               GSynthesisBlock(last_channels, channels, blur_filter, dlatent_size, gain, use_wscale, use_noise, use_pixel_norm, use_instance_norm, use_styles, act)))
            last_channels = channels
        self.torgb = MyConv2d(channels, num_channels, 1, gain=1, use_wscale=use_wscale)
        self.blocks = nn.ModuleDict(OrderedDict(blocks))
        
    def forward(self, dlatents_in):
        # Input: Disentangled latents (W) [minibatch, num_layers, dlatent_size].
        # lod_in = tf.cast(tf.get_variable('lod', initializer=np.float32(0), trainable=False), dtype)
        batch_size = dlatents_in.size(0)       
        for i, m in enumerate(self.blocks.values()):
            if i == 0:
                x = m(dlatents_in[:, 2*i:2*i+2])
            else:
                x = m(x, dlatents_in[:, 2*i:2*i+2])
        rgb = self.torgb(x)
        return rgb


# ### Step 4. Define the Model (Image Generator)

# **4-a. data flow : z to image**

# In[25]:


g_all = nn.Sequential(OrderedDict([
    ('g_mapping', G_mapping()),
    ('g_synthesis', G_synthesis())    
]))


# > If latent z is put into g_mapping network, w is returned, and if the returned w is put into g_synthesis, an image is created. This process is chained sequentially and occurs one after another.

# **4-b. load pre-trained weight**

# In[29]:


#get_ipython().system('pwd')


# In[30]:


import os
#os.listdir('./ffhq-1024x1024-pretrained')


# In[32]:

EKO()
g_all.load_state_dict(torch.load('./karras2019stylegan-ffhq-1024x1024.for_g_all.pt'))


# ### Step 5. Test the Model

# **5-a. gpu setting**

# In[33]:


device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
g_all.eval()
g_all.to(device)


# In[44]:

EKO()

# **5-b. input setting - grid**

# In[45]:


nb_rows = 4
nb_cols = 5
nb_samples = nb_rows * nb_cols


# **5-c. input setting - latent z**

# In[46]:


latents = torch.randn(nb_samples, 512, device=device)


# In[47]:


latents


# In[48]:


latents.shape


# **5-d. show samples**

# In[49]:


import torchvision
import matplotlib.pyplot as plt


# In[50]:

EKO()
with torch.no_grad():
    imgs = g_all(latents)
    imgs = (imgs.clamp(-1, 1)+1)/2.0  # normalization to 0~1 range

    
EKOX(imgs.shape)
img = imgs[0].cpu().numpy().transpose(1,2,0)

EKOX(img.shape)
EKOX(TYPE(img))
image_size, margin=512, 12

mtcnn = MTCNN(image_size=image_size, margin=margin)
resnet = InceptionResnetV1(pretrained='vggface2').eval().cuda()
from PIL import Image
img = Image.fromarray((img*255).astype(np.uint8))


if False : 
     # Get cropped and prewhitened image tensor
    img_cropped = mtcnn(img)
    EKOX(img_cropped.shape)
    EKOX(TYPE(img_cropped))

    unwhit = lambda x : ((x+1)/2*255).astype(int)

    #plt.imshow(unwhit(img_cropped.permute(1,2,0).detach().numpy())); plt.show()
    EKO()

    # Calculate embedding (unsqueeze to add batch dimension)
    img_embedding = resnet(img_cropped.unsqueeze(0))

    # Or, if using for VGGFace2 classification
    resnet.classify = True
    img_probs = resnet(img_cropped.unsqueeze(0))

EKOX(TYPE(imgs[0]))

wi = imgs[0] * 2 - 1
img_embedding = resnet(wi.unsqueeze(0))
# Or, if using for VGGFace2 classification
resnet.classify = True
img_probs = resnet(wi.unsqueeze(0))
sys.exit(0)

imgs = imgs.cpu()

imgs = torchvision.utils.make_grid(imgs, nrow=nb_cols)

plt.figure(figsize=(15,6))
plt.imshow(imgs.permute(1,2,0).detach().numpy())
plt.axis('off')
plt.show()


# ### Step 6. Control Latent Vector

# **6-a. first random latent vector + generate first image**

# In[40]:


latent1 = torch.randn(1, 512, device=device)
img1 = g_all(latent1)
img1 = img1.clamp(-1,1)+1/2.0
img1 = img1.cpu()

img1.shape


# In[41]:


plt.imshow(img1.squeeze().permute(1,2,0).detach().numpy()) # drop batch (4dim -> 3dim)
plt.axis('off')
plt.show()


# **6-b. second random latent vector + generate second image**

# In[42]:


latent2 = torch.randn(1, 512, device=device)
img2 = g_all(latent2)
img2 = img2.clamp(-1,1)+1/2.0
img2 = img2.cpu()

img2.shape


# In[43]:


plt.imshow(img2.squeeze().permute(1,2,0).detach().numpy()) # drop batch (4dim -> 3dim)
plt.axis('off')
plt.show()


# **6-c. half `z` + half `z`**

# In[ ]:


new_img = g_all(latent1*0.5 + latent2*0.5)
new_img = new_img.clamp(-1,1)+1/2.0
new_img = new_img.cpu()

new_img.shape


# In[ ]:


plt.imshow(new_img.squeeze().permute(1,2,0).detach().numpy()) 
plt.axis('off')
plt.show()


# **6-d. half `w` + half `w`**

# By the way, we actually have a w vector that passed through the G_mapping network. Let's try it.

# In[ ]:


g_mapping = g_all[0] # We can extract mapping network like this.
g_mapping


# In[ ]:


g_synthesis = g_all[1]# Similarly, synthesis network can be extracted like this.


# In[ ]:


w_1 = g_mapping(latent1)
w_2 = g_mapping(latent2)


# The results through the MLP mapping network are as follows.

# In[ ]:


print(latent1.size(), w_1.size())
print(latent2.size(), w_2.size())


# then, let's convert to image (half + half)

# In[ ]:


img3 = g_synthesis(w_1*0.5 + w_2*0.5)
img3 = img3.clamp(-1,1)+1/2.0
img3 = img3.cpu()

img3.shape


# In[ ]:


plt.imshow(img3.squeeze().permute(1,2,0).detach().numpy()) 
plt.axis('off')
plt.show()


# Yes! I think this looks more like a `half+half`
# 
# And It is a really surprising result that it is estimated to be in the middle even by age.

# **6-e. Image Interpolation Comparison**

# In[ ]:


itp_imgs = []

with torch.no_grad():
    for a in np.linspace(0, 1, 10):
        z = ((1-a) * latent1) + (a * latent2)
        result = g_all(z)
        result = result.clamp(-1,1)+1/2.0
        result = result.cpu()
        itp_imgs.append(result)


# In[ ]:


itp_imgs[0].size()


# In[ ]:


itp_imgs = torch.cat(itp_imgs)
itp_imgs.size()


# In[ ]:


grid_img = torchvision.utils.make_grid(itp_imgs, nrow=5)
grid_img.size()


# In[ ]:


plt.imshow(grid_img.permute(1,2,0).detach().numpy())
plt.axis('off')
plt.show()


# In[ ]:


itp_imgs2 = []

with torch.no_grad():
    for a in np.linspace(0, 1, 10):
        w = ((1-a) * w_1) + (a * w_2)
        result2 = g_synthesis(w)
        result2 = result2.clamp(-1,1)+1/2.0
        result2 = result2.cpu()
        itp_imgs2.append(result2)


# In[ ]:


itp_imgs2[0].size()


# In[ ]:


itp_imgs2 = torch.cat(itp_imgs2)
itp_imgs2.size()


# In[ ]:


grid_img2 = torchvision.utils.make_grid(itp_imgs2, nrow=5)
grid_img2.size()


# In[ ]:


plt.imshow(grid_img2.permute(1,2,0).detach().numpy())
plt.axis('off')
plt.show()


# Yes. It's so much more natural! Here we can see the strengths of stylegan. The traditional image generation model immediately generates an image from a random vector(gaussian distribution) z. That have showed how high the degree of freedom is, in other words, the low degree of feature separation(**It is said to be entangled**). stylegan captured this core 'style' through the mapping network, and we confirmed this through the interpolation results. 
# 
# in Abstract..
# > The new generator improves the state-of-the-art in terms of traditional distribution quality metrics, leads to demonstrably better interpolation properties, and also better disentangles the latent factors of variation. To quantify interpolation quality and disentanglement, we propose two new, automated methods that are applicable to any generator architecture. 
