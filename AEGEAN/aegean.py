import os
import sys
import time
import numpy as np

import torch
# torch.autograd.set_detect_anomaly(True)
from torch.autograd import Variable


from .init import init
from .utils import *
from .plot import *
from .models import *


try:
    # to use with `$ tensorboard --logdir runs`
    from torch.utils.tensorboard import SummaryWriter
    do_tensorboard = True
except:  # ImportError:
    do_tensorboard = False
    print("Failed loading Tensorboard.")

def learn(opt, run_dir="./runs"):
    os.makedirs(run_dir, exist_ok=True)
    path_data = os.path.join(run_dir, opt.run_path)
    if not os.path.isdir(path_data):
        os.makedirs(path_data, exist_ok=True)
        do_learn(opt)

def do_learn(opt, run_dir="./runs"):
    print('Starting ', opt.run_path)
    path_data = os.path.join(run_dir, opt.run_path)
    # ----------
    #  Tensorboard
    # ----------
    if do_tensorboard:
        # stats are stored in "runs", within subfolder opt.run_path.
        writer = SummaryWriter(log_dir=path_data)

    # Create a time tag
    import datetime
    try:
        tag = datetime.datetime.now().isoformat(sep='_', timespec='seconds')
    except TypeError:
        # Python 3.5 and below
        # 'timespec' is an invalid keyword argument for this function
        tag = datetime.datetime.now().replace(microsecond=0).isoformat(sep='_')
    tag = tag.replace(':', '-')


    # Configure data loader
    dataloader = load_data(opt.datapath, opt.img_size, opt.batch_size,
                           rand_hflip=opt.rand_hflip, rand_affine=opt.rand_affine)

    # Loss functions
    # https://pytorch.org/docs/stable/nn.html?highlight=bcewithlogitsloss#torch.nn.BCEWithLogitsLoss
    adversarial_loss = torch.nn.BCEWithLogitsLoss()  # eq. 8 in https://arxiv.org/pdf/1701.00160.pdf
    if opt.do_SSIM:
        from pytorch_msssim import NMSSSIM

        E_loss = NMSSSIM(window_size=opt.window_size, val_range=1., size_average=True, channel=3, normalize=True)
    else:
        E_loss = torch.nn.MSELoss(reduction='sum')
    # MSE_loss = torch.nn.MSELoss(reduction='sum')
    sigmoid = torch.nn.Sigmoid()

    # Initialize generator and discriminator
    generator = Generator(opt)
    discriminator = Discriminator(opt)
    encoder = Encoder(opt)
    if opt.latent_threshold>0.:
        hs = torch.nn.Hardshrink(lambd=opt.latent_threshold)

    if opt.verbose:
        print_network(generator)
        print_network(discriminator)
        print_network(encoder)

    use_cuda = True if torch.cuda.is_available() else False
    if use_cuda:
        #print("Nombre de GPU : ",torch.cuda.device_count())
        print("Running on GPU : ", torch.cuda.get_device_name())
        # if torch.cuda.device_count() > opt.GPU:
        #     torch.cuda.set_device(opt.GPU)
        generator.cuda()
        discriminator.cuda()
        adversarial_loss.cuda()
        encoder.cuda()
        # MSE_loss.cuda()
        E_loss.cuda()

        Tensor = torch.cuda.FloatTensor
    else:
        print("Running on CPU ")
        Tensor = torch.FloatTensor

    # Initialize weights
    if opt.init_weight:
        generator.apply(weights_init_normal)
        discriminator.apply(weights_init_normal)
        encoder.apply(weights_init_normal)

    # Optimizers
    if False:
        # https://pytorch.org/docs/stable/optim.html#torch.optim.RMSprop
        opts = dict(momentum=1-opt.beta1, alpha=opt.beta2)
        optimizer = torch.optim.RMSprop
    else:
        # https://pytorch.org/docs/stable/optim.html#torch.optim.Adam
        opts = dict(betas=(opt.beta1, opt.beta2))
        optimizer = torch.optim.Adam

    optimizer_G = optimizer(generator.parameters(), lr=opt.lrG, **opts)
    optimizer_D = optimizer(discriminator.parameters(), lr=opt.lrD, **opts)
    if opt.do_joint:
        import itertools
        optimizer_E = optimizer(itertools.chain(encoder.parameters(), generator.parameters()), lr=opt.lrE, **opts)
    else:
        optimizer_E = optimizer(encoder.parameters(), lr=opt.lrE, **opts)

    # ----------
    #  Training
    # ----------

    nb_batch = len(dataloader)

    hist = init_hist(opt.n_epochs, nb_batch)


    def gen_z(threshold=opt.latent_threshold, bandwidth=opt.latent_bandwidth):
        if bandwidth==0:
            z = np.random.normal(0, 1, (opt.batch_size, opt.latent_dim))
        else:
            z0 = np.random.normal(0, 1, (1, opt.latent_dim))
            z = z0 + bandwidth * np.random.normal(0, 1, (opt.batch_size, opt.latent_dim))
            z /= z.std() # TODO: could work without that

        if threshold > 0:
            z[np.abs(z)<threshold] = 0.
        z = Variable(Tensor(z), requires_grad=False)
        return z

    def gen_noise(real_imgs):
        v_noise = np.random.normal(0, 1, real_imgs.shape) # one random image
        v_noise *= np.abs(np.random.normal(0, 1, (real_imgs.shape[0], opt.channels, 1, 1))) # one contrast value per image
        noise = Variable(Tensor(v_noise), requires_grad=False)
        return noise

    # Vecteur z fixe pour faire les samples
    fixed_noise = gen_z()
    real_imgs_samples = None

    z_zeros = Variable(Tensor(opt.batch_size, opt.latent_dim).fill_(0), requires_grad=False)
    z_ones = Variable(Tensor(opt.batch_size, opt.latent_dim).fill_(1), requires_grad=False)
    valid = Variable(Tensor(opt.batch_size, 1).fill_(1), requires_grad=False)
    fake = Variable(Tensor(opt.batch_size, 1).fill_(0), requires_grad=False)

    # Adversarial ground truths
    valid_smooth = Variable(Tensor(opt.batch_size, 1).fill_(float(np.random.uniform(opt.valid_smooth, 1.0, 1))), requires_grad=False)


    t_total = time.time()
    for j, epoch in enumerate(range(1, opt.n_epochs + 1)):
        t_epoch = time.time()
        for i, (imgs, _) in enumerate(dataloader):
            t_batch = time.time()

            # ---------------------
            #  Train Encoder
            # ---------------------
            for p in generator.parameters():
                p.requires_grad = opt.do_joint
            for p in encoder.parameters():
                p.requires_grad = True
            for p in discriminator.parameters():
                p.requires_grad = False  # to avoid learning

            real_imgs = Variable(imgs.type(Tensor), requires_grad=False)

            # init samples used to visualize performance of the AE
            if real_imgs_samples is None:
                real_imgs_samples = real_imgs[:opt.N_samples]

            # add noise here to real_imgs
            real_imgs_ = real_imgs * 1.
            if opt.E_noise > 0: real_imgs_ += opt.E_noise * gen_noise(real_imgs)

            z_imgs = encoder(real_imgs_)
            if opt.latent_threshold>0:
                z_imgs = hs(z_imgs)
            decoded_imgs = generator(z_imgs)

            optimizer_E.zero_grad()

            # Loss measures Encoder's ability to generate vectors suitable with the generator
            e_loss = E_loss(real_imgs, decoded_imgs)
            # energy = 1. # E_loss(real_imgs, zero_target)  # normalize on the energy of imgs
            # if opt.do_joint:
            #     e_loss = E_loss(real_imgs, decoded_imgs) / energy
            # else:
            #     e_loss = E_loss(real_imgs, decoded_imgs.detach()) / energy

            if opt.lambdaE > 0:
                # We wish to make sure the z_imgs get closer to a gaussian
                e_loss += opt.lambdaE * (torch.sum(z_imgs)/opt.batch_size/opt.latent_dim).pow(2)
                e_loss += opt.lambdaE * (torch.sum(z_imgs.pow(2))/opt.batch_size/opt.latent_dim-1).pow(2).pow(.5)

            # Backward
            e_loss.backward()
            optimizer_E.step()

            if opt.lrD > 0:
                # ---------------------
                #  Train Discriminator
                # ---------------------
                # Discriminator Requires grad, Encoder + Generator requires_grad = False
                for p in discriminator.parameters():
                    p.requires_grad = True
                for p in generator.parameters():
                    p.requires_grad = False  # to avoid computation
                for p in encoder.parameters():
                    p.requires_grad = False  # to avoid computation

            # Configure input
            real_imgs = Variable(imgs.type(Tensor), requires_grad=False)
            real_imgs_ = real_imgs * 1.
            if opt.D_noise > 0: real_imgs_ += opt.D_noise * gen_noise(real_imgs)
            if opt.do_insight: real_imgs_ = generator(encoder(real_imgs_))

            # Discriminator decision (in logit units)
            logit_d_x = discriminator(real_imgs_)

            if opt.lrD > 0:
                # ---------------------
                #  Train Discriminator
                # ---------------------
                if opt.GAN_loss == 'wasserstein':
                    # weight clipping
                    for p in discriminator.parameters():
                        p.data.clamp_(-0.01, 0.01)

                optimizer_D.zero_grad()

                # Measure discriminator's ability to classify real from generated samples
                if opt.GAN_loss == 'ian':
                    # eq. 14 in https://arxiv.org/pdf/1701.00160.pdf
                    real_loss = - torch.sum(1 / (1. - 1/sigmoid(logit_d_x)))
                elif opt.GAN_loss == 'wasserstein':
                    real_loss = torch.mean(torch.abs(valid_smooth - sigmoid(logit_d_x)))
                elif opt.GAN_loss == 'alternative':
                    # https://www.inference.vc/an-alternative-update-rule-for-generative-adversarial-networks/
                    real_loss = - torch.sum(torch.log(sigmoid(logit_d_x)))
                elif opt.GAN_loss == 'alternativ2':
                    # https://www.inference.vc/an-alternative-update-rule-for-generative-adversarial-networks/
                    real_loss = - torch.sum(torch.log(sigmoid(logit_d_x) / (1. - sigmoid(logit_d_x))))
                elif opt.GAN_loss == 'alternativ3':
                    # to maximize D(x), we minimize  - sum(logit_d_x)
                    real_loss = - torch.sum(logit_d_x)
                elif opt.GAN_loss == 'original':
                    real_loss = adversarial_loss(logit_d_x, valid_smooth)
                else:
                    print ('GAN_loss not defined', opt.GAN_loss)

                # Backward
                real_loss.backward()

            # Generate a batch of fake images and learn the discriminator to treat them as such
            z = gen_z()
            if opt.latent_threshold>0:
                z = hs(z)
            gen_imgs = generator(z)
            # Discriminator decision for fake data
            gen_imgs_ = gen_imgs * 1.
            if opt.D_noise > 0: gen_imgs_ += opt.D_noise * gen_noise(real_imgs)

            logit_d_fake = discriminator(gen_imgs_.detach())
            if opt.lrD > 0:
                # Measure discriminator's ability to classify real from generated samples
                if opt.GAN_loss == 'wasserstein':
                    fake_loss = torch.mean(sigmoid(logit_d_fake))
                elif opt.GAN_loss == 'alternative':
                    # https://www.inference.vc/an-alternative-update-rule-for-generative-adversarial-networks/
                    fake_loss = - torch.sum(torch.log(1-sigmoid(logit_d_fake)))
                elif opt.GAN_loss == 'alternativ2':
                    # https://www.inference.vc/an-alternative-update-rule-for-generative-adversarial-networks/
                    fake_loss = torch.sum(torch.log(sigmoid(logit_d_fake) / (1. - sigmoid(logit_d_fake))))
                elif opt.GAN_loss == 'alternativ3':
                    # to minimize D(G(z)), we minimize sum(logit_d_fake)
                    fake_loss = torch.sum(logit_d_fake)
                elif opt.GAN_loss in ['original', 'ian']:
                    fake_loss = adversarial_loss(logit_d_fake, fake)
                else:
                    print ('GAN_loss not defined', opt.GAN_loss)

                # Backward
                fake_loss.backward()

            if opt.lrD > 0:
                d_loss = real_loss + fake_loss

                optimizer_D.step()

            if opt.lrG > 0:
                # -----------------
                #  Train Generator
                # -----------------
                # TODO : optimiser la distance z - E(G(z))
                for p in generator.parameters():
                    p.requires_grad = True
                for p in discriminator.parameters():
                    p.requires_grad = False  # to avoid computation
                for p in encoder.parameters():
                    p.requires_grad = False  # to avoid computation

            # Generate a batch of fake images
            z = gen_z()
            gen_imgs = generator(z)
            # New discriminator decision (since we just updated D)
            gen_imgs_ = gen_imgs * 1.
            if opt.G_noise > 0: gen_imgs_ += opt.G_noise * gen_noise(real_imgs)

            logit_d_g_z = discriminator(gen_imgs_)

            if opt.lrG > 0:
                optimizer_G.zero_grad()

                # Loss measures generator's ability to fool the discriminator
                if opt.GAN_loss == 'ian':
                    # eq. 14 in https://arxiv.org/pdf/1701.00160.pdf
                    # https://en.wikipedia.org/wiki/Logit
                    g_loss = - torch.sum(sigmoid(logit_d_g_z)/(1 - sigmoid(logit_d_g_z)))
                elif opt.GAN_loss == 'wasserstein':
                    g_loss = torch.mean(torch.abs(valid - sigmoid(logit_d_g_z)))
                elif opt.GAN_loss == 'alternative':
                    # https://www.inference.vc/an-alternative-update-rule-for-generative-adversarial-networks/
                    g_loss = - torch.sum(torch.log(sigmoid(logit_d_g_z)))
                elif opt.GAN_loss == 'alternativ2':
                    # https://www.inference.vc/an-alternative-update-rule-for-generative-adversarial-networks/
                    g_loss = - torch.sum(torch.log(sigmoid(logit_d_g_z) / (1. - sigmoid(logit_d_g_z))))
                    # g_loss = torch.sum(torch.log(1./sigmoid(logit_d_g_z) - 1.))
                elif opt.GAN_loss == 'alternativ3':
                    # to maximize D(G(z)), we minimize - sum(logit_d_fake)
                    g_loss = - torch.sum(logit_d_g_z)
                elif opt.GAN_loss == 'original':
                    g_loss = adversarial_loss(logit_d_g_z, valid)
                else:
                    print ('GAN_loss not defined', opt.GAN_loss)

                # Backward
                g_loss.backward()
                optimizer_G.step()


            # -----------------
            #  Recording stats
            # -----------------
            if opt.lrG > 0:
                # Compensation pour le BCElogits
                d_fake = sigmoid(logit_d_fake)
                d_x = sigmoid(logit_d_x)
                d_g_z = sigmoid(logit_d_g_z)
                print(
                    "%s [Epoch %d/%d] [Batch %d/%d] [E loss: %f] [D loss: %f] [G loss: %f] [D(x) %f] [D(G(z)) %f] [D(G(z)) %f] [Time: %fs]"
                    % (opt.run_path, epoch, opt.n_epochs, i+1, len(dataloader), e_loss.item(), d_loss.item(), g_loss.item(), torch.mean(d_x), torch.mean(d_fake), torch.mean(d_g_z), time.time()-t_batch)
                )
                # Save Losses and scores for Tensorboard
                save_hist_batch(hist, i, j, g_loss, d_loss, e_loss, d_x, d_g_z)
            else:
                print(
                    "%s [Epoch %d/%d] [Batch %d/%d] [E loss: %f] [Time: %fs]"
                    % (opt.run_path, epoch, opt.n_epochs, i+1, len(dataloader), e_loss.item(), time.time()-t_batch)
                )

        if do_tensorboard:
            # Tensorboard save
            writer.add_scalar('loss/E', e_loss.item(), global_step=epoch)
            writer.add_histogram('coeffs/z', z, global_step=epoch)
            try:
                writer.add_histogram('coeffs/E_x', z_imgs, global_step=epoch)
            except:
                pass
            writer.add_histogram('image/x', real_imgs, global_step=epoch)
            try:
                writer.add_histogram('image/E_G_x', decoded_imgs, global_step=epoch)
            except:
                pass
            try:
                writer.add_histogram('image/G_z', gen_imgs, global_step=epoch)
            except:
                pass
            if opt.lrG > 0:
                writer.add_scalar('loss/G', g_loss.item(), global_step=epoch)
                # writer.add_scalar('score/D_fake', hist["d_fake_mean"][i], global_step=epoch)
                writer.add_scalar('score/D_g_z', hist["d_g_z_mean"][i], global_step=epoch)
                # try:
                #     writer.add_histogram('D_G_z', d_g_z, global_step=epoch,
                #                          bins=np.linspace(0, 1, 20))
                # except:
                #     pass
            if opt.lrD > 0:
                writer.add_scalar('loss/D', d_loss.item(), global_step=epoch)

                writer.add_scalar('score/D_x', hist["d_x_mean"][i], global_step=epoch)

                # writer.add_scalar('d_x_cv', hist["d_x_cv"][i], global_step=epoch)
                # writer.add_scalar('d_g_z_cv', hist["d_g_z_cv"][i], global_step=epoch)
                # try:
                #     writer.add_histogram('D_x', d_x, global_step=epoch,
                #                      bins=np.linspace(0, 1, 20))
                # except:
                #     pass

            # inception score
            # IS, _ = get_inception_score(gen_imgs, cuda=use_cuda, batch_size=opt.batch_size//4, resize=True, splits=1)
            # writer.add_scalar('InceptionScore', IS, global_step=epoch)

            # writer.add_scalar('D_x/max', hist["D_x_max"][j], global_step=epoch)
            # writer.add_scalar('D_x/min', hist["D_x_min"][j], global_step=epoch)
            # writer.add_scalar('D_G_z/min', hist["D_G_z_min"][j], global_step=epoch)
            # writer.add_scalar('D_G_z/max', hist["D_G_z_max"][j], global_step=epoch)

            # Save samples
            if epoch % opt.sample_interval == 0:
                """
                Use generator model and noise vector to generate images.
                Save them to tensorboard
                """
                generator.eval()
                gen_imgs = generator(fixed_noise)
                grid = torchvision.utils.make_grid(gen_imgs, normalize=True, nrow=8, range=(0, 1))
                writer.add_image('Generated images', grid, epoch)
                generator.train()


                """
                Use auto-encoder model and original images to generate images.
                Save them to tensorboard

                """
                grid_imgs = torchvision.utils.make_grid(real_imgs_samples, normalize=True, nrow=8, range=(0, 1))
                writer.add_image('Images/original', grid_imgs, epoch)

                generator.eval()
                encoder.eval()
                enc_imgs = encoder(real_imgs_samples)
                dec_imgs = generator(enc_imgs)
                grid_dec = torchvision.utils.make_grid(dec_imgs, normalize=True, nrow=8, range=(0, 1))
                writer.add_image('Images/auto-encoded', grid_dec, epoch)
                generator.train()
                encoder.train()


        if epoch % opt.sample_interval == 0 :
            sampling(fixed_noise, generator, path_data, epoch, tag)
            # do_plot(hist, start_epoch, epoch)

        print("[Epoch Time: ", time.time() - t_epoch, "s]")

    t_final = time.gmtime(time.time() - t_total)
    print("[Total Time: ", t_final.tm_mday - 1, "j:",
          time.strftime("%Hh:%Mm:%Ss", t_final), "]", sep='')

    if do_tensorboard:
        writer.close()
