#!/usr/bin/env python3
# Copyright 2004-present Facebook. All Rights Reserved.

import argparse
import json
import logging
import os
import random
import time
import torch
import torch.nn.functional as F
import numpy as np
import math

os.environ["KMP_DUPLICATE_LIB_OK"]="TRUE"

import deep_sdf
import deep_sdf.workspace as ws

def get_spec_with_default(specs, key, default):
    try:
        return specs[key]
    except KeyError:
        return default

def reconstruct(
    decoder,
    num_iterations,
    latent_size,
    test_sdf,
    stat,
    clamp_dist,
    num_samples=30000,
    lr=5e-2,
    l2reg=False):

    def adjust_learning_rate(
        initial_lr, optimizer, num_iterations, decreased_by, adjust_lr_every):
        lr = initial_lr * ((1 / decreased_by) ** (num_iterations // adjust_lr_every))
        for param_group in optimizer.param_groups:
            param_group["lr"] = lr

    decreased_by = 10
    adjust_lr_every = int(num_iterations / 2)

    if type(stat) == type(0.1):
        latent = torch.ones(1, latent_size).normal_(mean=0, std=stat).cuda()
    else:
        latent = torch.normal(stat[0].detach(), stat[1].detach()).cuda()
    # lat_vec = torch.nn.Embedding(1, latent_size, max_norm=code_bound).cuda()
    # torch.nn.init.normal_(
    #     lat_vec.weight.data,
    #     0.0,
    #     latent_var
    # )
    # latent = lat_vec(torch.zeros(1).long().cuda()).clone().detach()
    latent.requires_grad = True

    optimizer = torch.optim.Adam([latent], lr=lr)

    loss_num = 0
    loss_l1 = torch.nn.L1Loss()

    for e in range(num_iterations):
        decoder.eval()
        # sdf_data = deep_sdf.data.unpack_sdf_samples_from_ram(
        #     test_sdf, num_samples
        # ).cuda()
        sdf_data, _ = deep_sdf.data.get_sdf_samples_test(test_sdf, num_samples)
        sdf_data = sdf_data.cuda()
        xyz = sdf_data[:, 0:3]
        sdf_gt = sdf_data[:, 3].unsqueeze(1)

        sdf_gt = torch.clamp(sdf_gt, -clamp_dist, clamp_dist)

        adjust_learning_rate(lr, optimizer, e, decreased_by, adjust_lr_every)

        optimizer.zero_grad()

        latent_inputs = latent.expand(num_samples, -1)

        inputs = torch.cat([latent_inputs, xyz], 1).cuda()

        pred_sdf = decoder(inputs)

        # TODO: why is this needed?
        if e == 0:
            pred_sdf = decoder(inputs)

        pred_sdf = torch.clamp(pred_sdf, -clamp_dist, clamp_dist)

        loss = loss_l1(pred_sdf, sdf_gt)
        if l2reg:
            loss += 1e-4 * torch.mean(latent.pow(2))
        loss.backward()
        optimizer.step()
        sdf_error = loss.item()
        print("epoch {}, sdf_loss = {:.9e}".format(e, sdf_error))

        if e % 100 == 0:
            logging.debug(e)
            logging.debug(loss.item())
            # logging.debug(latent.norm().item())
        loss_num = loss.item()

    return loss_num, latent


if __name__ == "__main__":
    import sys
    arg_parser = argparse.ArgumentParser(
        description="Use a trained DeepSDF decoder to reconstruct a shape given SDF "
        + "samples."
    )
    arg_parser.add_argument(
        "--experiment",
        "-e",
        dest="experiment_directory",
        required=True,
        help="The experiment directory which includes specifications and saved model "
        + "files to use for reconstruction",
    )
    arg_parser.add_argument(
        "--checkpoint",
        "-c",
        dest="checkpoint",
        default="latest",
        help="The checkpoint weights to use. This can be a number indicated an epoch "
        + "or 'latest' for the latest weights (this is the default)",
    )
    arg_parser.add_argument(
        "--data",
        "-d",
        dest="data_source",
        required=True,
        help="The data source directory.",
    )
    arg_parser.add_argument(
        "--split",
        "-s",
        dest="split_filename",
        required=True,
        help="The split to reconstruct.",
    )
    arg_parser.add_argument(
        "--iters",
        dest="iterations",
        type=int,
        default=500,
        help="The number of iterations of latent code optimization to perform.",
    )
    arg_parser.add_argument(
        "--skip",
        dest="skip",
        action="store_true",
        help="Skip meshes which have already been reconstructed.",
    )
    arg_parser.add_argument(
        "--seed",
        dest="seed",
        default=10,
        help="random seed",
    )
    arg_parser.add_argument(
        "--resolution",
        dest="resolution",
        type=int,
        default=128,
        help="Marching cube resolution.",
    )

    use_octree_group = arg_parser.add_mutually_exclusive_group()
    use_octree_group.add_argument(
        '--octree',
        dest='use_octree',
        action='store_true',
        help='Use octree to accelerate inference. Octree is recommend for most object categories '
             'except those with thin structures like planes'
    )
    use_octree_group.add_argument(
        '--no_octree',
        dest='use_octree',
        action='store_false',
        help='Don\'t use octree to accelerate inference. Octree is recommend for most object categories '
             'except those with thin structures like planes'
    )
    sys.argv = [r"reconstruct_ndf.py",
                "--experiment", r"examples/all",
                "--split", "examples/all/test.json",
                "--data", r"\\SEUVCL-DATA-03\Data03Training\0518_4dsdf_yxh\data_2"
                ]

    deep_sdf.add_common_args(arg_parser)

    args = arg_parser.parse_args()

    random.seed(31359)
    torch.random.manual_seed(31359)
    np.random.seed(31359)

    deep_sdf.configure_logging(args)

    def empirical_stat(latent_vecs, indices):
        lat_mat = torch.zeros(0).cuda()
        for ind in indices:
            lat_mat = torch.cat([lat_mat, latent_vecs[ind]], 0)
        mean = torch.mean(lat_mat, 0)
        var = torch.var(lat_mat, 0)
        return mean, var

    specs_filename = os.path.join(args.experiment_directory, "specs.json")

    if not os.path.isfile(specs_filename):
        raise Exception(
            'The experiment directory does not include specifications file "specs.json"'
        )

    specs = json.load(open(specs_filename))

    arch = __import__("networks." + specs["NetworkArch"], fromlist=["Decoder"])

    latent_size = specs["CodeLength"]

    decoder = arch.Decoder(latent_size, **specs["NetworkSpecs"])

    decoder = torch.nn.DataParallel(decoder)

    saved_model_state = torch.load(
        os.path.join(
            args.experiment_directory, ws.model_params_subdir, args.checkpoint + ".pth"
        )
    )
    saved_model_epoch = saved_model_state["epoch"]

    decoder.load_state_dict(saved_model_state["model_state_dict"])

    decoder = decoder.module.cuda()

    with open(args.split_filename, "r") as f:
        split = json.load(f)

    # npz_filenames = deep_sdf.data.get_instance_filenames(args.data_source, split)
    npz_filenames = []
    normalizationfiles = []
    normalized = True
    for dataset in split:
        for class_name in split[dataset]:
            for instance_name in split[dataset][class_name]:
                instance_filename = os.path.join(
                    dataset, "Processed", class_name, instance_name + ".npz"
                )
                if normalized:
                    normalization_params_filename = os.path.join(
                        dataset, "NormalizationParameters", class_name, instance_name + ".npz"
                    )
                    normalizationfiles += [normalization_params_filename]
                if not os.path.isfile(
                        os.path.join(args.data_source, instance_filename)
                ):
                    # raise RuntimeError(
                    #     'Requested non-existent file "' + instance_filename + "'"
                    # )
                    logging.warning(
                        "Requested non-existent file '{}'".format(instance_filename)
                    )
                npz_filenames += [instance_filename]


    # random.shuffle(npz_filenames)
    npz_filenames = sorted(npz_filenames)
    normalizationfiles = sorted(normalizationfiles)

    logging.debug(decoder)

    err_sum = 0.0
    repeat = 1
    save_latvec_only = False
    rerun = 0

    reconstruction_dir = os.path.join(
        args.experiment_directory, "test_results", str(saved_model_epoch)
    )

    if not os.path.isdir(reconstruction_dir):
        os.makedirs(reconstruction_dir)

    reconstruction_meshes_dir = os.path.join(
        reconstruction_dir, ws.reconstruction_meshes_subdir
    )
    if not os.path.isdir(reconstruction_meshes_dir):
        os.makedirs(reconstruction_meshes_dir)

    reconstruction_codes_dir = os.path.join(
        reconstruction_dir, ws.reconstruction_codes_subdir
    )
    if not os.path.isdir(reconstruction_codes_dir):
        os.makedirs(reconstruction_codes_dir)

    clamping_function = None
    if specs["NetworkArch"] == "deep_sdf_decoder":
        clamping_function = lambda x : torch.clamp(x, -specs["ClampingDistance"], specs["ClampingDistance"])
    elif specs["NetworkArch"] == "deep_implicit_template_decoder":
        # clamping_function = lambda x: x * specs["ClampingDistance"]
        clamping_function = lambda x : torch.clamp(x, -specs["ClampingDistance"], specs["ClampingDistance"])
    # clamping_function = lambda x : torch.clamp(x, -specs["ClampingDistance"], specs["ClampingDistance"])

    latent_var = get_spec_with_default(specs, "CodeInitStdDev", 1.0) / math.sqrt(latent_size)
    code_bound = get_spec_with_default(specs, "CodeBound", None)
    
    for ii, npz in enumerate(npz_filenames):

        if "npz" not in npz:
            continue

        pcd_filename = os.path.join(args.data_source, npz)  # \data\pcd\synthetic\test\xxx.obj
        norm_filename = os.path.join(args.data_source, normalizationfiles[ii])
        normalization_params = np.load(norm_filename)
        # full_filename = os.path.join(args.data_source, ws.sdf_samples_subdir, npz)

        logging.debug("loading {}".format(npz))

        # data_sdf = deep_sdf.data.read_sdf_samples_into_ram(full_filename)


        for k in range(repeat):

            if rerun > 1:
                mesh_filename = os.path.join(
                    reconstruction_meshes_dir, npz[:-4] + "-" + str(k + rerun)
                )
                latent_filename = os.path.join(
                    reconstruction_codes_dir, npz[:-4] + "-" + str(k + rerun) + ".pth"
                )
            else:
                mesh_filename = os.path.join(reconstruction_meshes_dir, npz[:-4])
                latent_filename = os.path.join(
                    reconstruction_codes_dir, npz[:-4] + ".pth"
                )

            if (
                args.skip
                and os.path.isfile(mesh_filename + ".ply")
                and os.path.isfile(latent_filename)
            ):
                continue

            logging.info("reconstructing {}".format(npz))

            start = time.time()
            if not os.path.isfile(latent_filename):
                err, latent = reconstruct(
                    decoder,
                    int(args.iterations),
                    latent_size,
                    pcd_filename,
                    0.01,  # [emp_mean,emp_var],
                    0.1,
                    num_samples=8000,
                    lr=5e-2,
                    l2reg=True
                )
                logging.info("reconstruct time: {}".format(time.time() - start))
                logging.info("reconstruction error: {}".format(err))
                err_sum += err
                # logging.info("current_error avg: {}".format((err_sum / (ii + 1))))
                # logging.debug(ii)

                # logging.debug("latent: {}".format(latent.detach().cpu().numpy()))
            else:
                logging.info("loading from " + latent_filename)
                latent = torch.load(latent_filename).squeeze(0)

            decoder.eval()

            if not os.path.exists(os.path.dirname(mesh_filename)):
                os.makedirs(os.path.dirname(mesh_filename))

            if not save_latvec_only:
                start = time.time()
                with torch.no_grad():
                    if args.use_octree:
                        deep_sdf.mesh.create_mesh_octree(
                            decoder, latent, mesh_filename, N=args.resolution, max_batch=int(2 ** 17),
                            clamp_func=clamping_function,
                        )
                    else:
                        deep_sdf.mesh.create_mesh(
                            decoder, latent, mesh_filename, N=args.resolution, max_batch=int(2 ** 17),
                            offset=normalization_params["offset"], scale=normalization_params["scale"],
                            Ti=normalization_params["Ti"]
                        )
                logging.debug("total time: {}".format(time.time() - start))

            if not os.path.exists(os.path.dirname(latent_filename)):
                os.makedirs(os.path.dirname(latent_filename))

            torch.save(latent.unsqueeze(0), latent_filename)
