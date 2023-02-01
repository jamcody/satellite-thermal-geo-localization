import faiss
import torch
import logging
import numpy as np
from tqdm import tqdm
from torchvision import transforms
from torch.utils.data import DataLoader
from torch.utils.data.dataset import Subset
from utils.plotting import save_heatmap_simulation, process_results_simulation
from h5_transformer import calc_overlap
from model.functional import calculate_psnr
import yaml
import os
from PIL import Image
import datasets_ws

def test_efficient_ram_usage(args, eval_ds, model, test_method="hard_resize"):
    """This function gives the same output as test(), but uses much less RAM.
    This can be useful when testing with large descriptors (e.g. NetVLAD) on large datasets (e.g. San Francisco).
    Obviously it is slower than test(), and can't be used with PCA.
    """

    model = model.eval()
    if test_method == "nearest_crop" or test_method == "maj_voting":
        distances = np.empty(
            [eval_ds.queries_num * 5, eval_ds.database_num], dtype=np.float32
        )
    else:
        distances = np.empty(
            [eval_ds.queries_num, eval_ds.database_num], dtype=np.float32
        )

    with torch.no_grad():
        if test_method == "nearest_crop" or test_method == "maj_voting":
            queries_features = np.ones(
                (eval_ds.queries_num * 5, args.features_dim), dtype="float32"
            )
        else:
            queries_features = np.ones(
                (eval_ds.queries_num, args.features_dim), dtype="float32"
            )
        logging.debug("Extracting queries features for evaluation/testing")
        queries_infer_batch_size = (
            1 if test_method == "single_query" else args.infer_batch_size
        )
        eval_ds.test_method = test_method
        queries_subset_ds = Subset(
            eval_ds,
            list(
                range(eval_ds.database_num,
                      eval_ds.database_num + eval_ds.queries_num)
            ),
        )
        queries_dataloader = DataLoader(
            dataset=queries_subset_ds,
            num_workers=args.num_workers,
            batch_size=queries_infer_batch_size,
            pin_memory=(args.device == "cuda"),
        )
        for inputs, indices in tqdm(queries_dataloader, ncols=100):
            if (
                test_method == "five_crops"
                or test_method == "nearest_crop"
                or test_method == "maj_voting"
            ):
                # shape = 5*bs x 3 x 480 x 480
                inputs = torch.cat(tuple(inputs))
            features = model(inputs.to(args.device))
            if test_method == "five_crops":  # Compute mean along the 5 crops
                features = torch.stack(torch.split(features, 5)).mean(1)
            if test_method == "nearest_crop" or test_method == "maj_voting":
                start_idx = (indices[0] - eval_ds.database_num) * 5
                end_idx = start_idx + indices.shape[0] * 5
                indices = np.arange(start_idx, end_idx)
                queries_features[indices, :] = features.cpu().numpy()
            else:
                queries_features[
                    indices.numpy() - eval_ds.database_num, :
                ] = features.cpu().numpy()

        queries_features = torch.tensor(
            queries_features).type(torch.float32).cuda()

        logging.debug("Extracting database features for evaluation/testing")
        # For database use "hard_resize", although it usually has no effect because database images have same resolution
        eval_ds.test_method = "hard_resize"
        database_subset_ds = Subset(eval_ds, list(range(eval_ds.database_num)))
        database_dataloader = DataLoader(
            dataset=database_subset_ds,
            num_workers=args.num_workers,
            batch_size=args.infer_batch_size,
            pin_memory=(args.device == "cuda"),
        )
        for inputs, indices in tqdm(database_dataloader, ncols=100):
            inputs = inputs.to(args.device)
            features = model(inputs)
            for pn, (index, pred_feature) in enumerate(zip(indices, features)):
                distances[:, index] = (
                    ((queries_features - pred_feature) ** 2).sum(1).cpu().numpy()
                )
        del features, queries_features, pred_feature

    predictions = distances.argsort(axis=1)[:, : max(args.recall_values)]

    if test_method == "nearest_crop":
        distances = np.array(
            [distances[row, index] for row, index in enumerate(predictions)]
        )
        distances = np.reshape(distances, (eval_ds.queries_num, 20 * 5))
        predictions = np.reshape(predictions, (eval_ds.queries_num, 20 * 5))
        for q in range(eval_ds.queries_num):
            # sort predictions by distance
            sort_idx = np.argsort(distances[q])
            predictions[q] = predictions[q, sort_idx]
            # remove duplicated predictions, i.e. keep only the closest ones
            _, unique_idx = np.unique(predictions[q], return_index=True)
            # unique_idx is sorted based on the unique values, sort it again
            predictions[q, :20] = predictions[q, np.sort(unique_idx)][:20]
        predictions = predictions[
            :, :20
        ]  # keep only the closer 20 predictions for each
    elif test_method == "maj_voting":
        distances = np.array(
            [distances[row, index] for row, index in enumerate(predictions)]
        )
        distances = np.reshape(distances, (eval_ds.queries_num, 5, 20))
        predictions = np.reshape(predictions, (eval_ds.queries_num, 5, 20))
        for q in range(eval_ds.queries_num):
            # votings, modify distances in-place
            top_n_voting("top1", predictions[q],
                         distances[q], args.majority_weight)
            top_n_voting("top5", predictions[q],
                         distances[q], args.majority_weight)
            top_n_voting("top10", predictions[q],
                         distances[q], args.majority_weight)

            # flatten dist and preds from 5, 20 -> 20*5
            # and then proceed as usual to keep only first 20
            dists = distances[q].flatten()
            preds = predictions[q].flatten()

            # sort predictions by distance
            sort_idx = np.argsort(dists)
            preds = preds[sort_idx]
            # remove duplicated predictions, i.e. keep only the closest ones
            _, unique_idx = np.unique(preds, return_index=True)
            # unique_idx is sorted based on the unique values, sort it again
            # here the row corresponding to the first crop is used as a
            # 'buffer' for each query, and in the end the dimension
            # relative to crops is eliminated
            predictions[q, 0, :20] = preds[np.sort(unique_idx)][:20]
        predictions = predictions[
            :, 0, :20
        ]  # keep only the closer 20 predictions for each query
    del distances

    # For each query, check if the predictions are correct
    positives_per_query = eval_ds.get_positives()
    # args.recall_values by default is [1, 5, 10, 20]
    recalls = np.zeros(len(args.recall_values))
    for query_index, pred in enumerate(predictions):
        for i, n in enumerate(args.recall_values):
            if np.any(np.in1d(pred[:n], positives_per_query[query_index])):
                recalls[i:] += 1
                break

    recalls = recalls / eval_ds.queries_num * 100
    recalls_str = ", ".join(
        [f"R@{val}: {rec:.1f}" for val,
            rec in zip(args.recall_values, recalls)]
    )
    return recalls, recalls_str


def test(args, eval_ds, model, model_db=None, test_method="hard_resize", pca=None):
    """Compute features of the given dataset and compute the recalls."""

    assert test_method in [
        "hard_resize",
        "single_query",
        "central_crop",
        "five_crops",
        "nearest_crop",
        "maj_voting",
    ], f"test_method can't be {test_method}"

    if args.efficient_ram_testing:
        if model_db is not None:
            raise NotImplementedError()
        return test_efficient_ram_usage(args, eval_ds, model, test_method)

    model = model.eval()
    if model_db is not None:
        model_db = model_db.eval()
    with torch.no_grad():
        logging.debug("Extracting database features for evaluation/testing")
        # For database use "hard_resize", although it usually has no effect because database images have same resolution
        eval_ds.test_method = "hard_resize"
        database_subset_ds = Subset(eval_ds, list(range(eval_ds.database_num)))
        database_dataloader = DataLoader(
            dataset=database_subset_ds,
            num_workers=args.num_workers,
            batch_size=args.infer_batch_size,
            pin_memory=(args.device == "cuda"),
        )

        if test_method == "nearest_crop" or test_method == "maj_voting":
            all_features = np.empty(
                (5 * eval_ds.queries_num + eval_ds.database_num, args.features_dim),
                dtype="float32",
            )
        else:
            all_features = np.empty(
                (len(eval_ds), args.features_dim), dtype="float32")

        for inputs, indices in tqdm(database_dataloader, ncols=100):
            if model_db is not None:
                features = model_db(inputs.to(args.device))
            else:
                features = model(inputs.to(args.device))
            features = features.cpu().numpy()
            if pca != None:
                features = pca.transform(features)
            all_features[indices.numpy(), :] = features

        logging.debug("Extracting queries features for evaluation/testing")
        queries_infer_batch_size = (
            1 if test_method == "single_query" else args.infer_batch_size
        )
        eval_ds.test_method = test_method
        queries_subset_ds = Subset(
            eval_ds,
            list(
                range(eval_ds.database_num,
                      eval_ds.database_num + eval_ds.queries_num)
            ),
        )
        queries_dataloader = DataLoader(
            dataset=queries_subset_ds,
            num_workers=args.num_workers,
            batch_size=queries_infer_batch_size,
            pin_memory=(args.device == "cuda"),
        )
        for inputs, indices in tqdm(queries_dataloader, ncols=100):
            if (
                test_method == "five_crops"
                or test_method == "nearest_crop"
                or test_method == "maj_voting"
            ):
                # shape = 5*bs x 3 x 480 x 480
                inputs = torch.cat(tuple(inputs))
            features = model(inputs.to(args.device))
            if test_method == "five_crops":  # Compute mean along the 5 crops
                features = torch.stack(torch.split(features, 5)).mean(1)
            features = features.cpu().numpy()
            if pca != None:
                features = pca.transform(features)

            if (
                test_method == "nearest_crop" or test_method == "maj_voting"
            ):  # store the features of all 5 crops
                start_idx = (
                    eval_ds.database_num +
                    (indices[0] - eval_ds.database_num) * 5
                )
                end_idx = start_idx + indices.shape[0] * 5
                indices = np.arange(start_idx, end_idx)
                all_features[indices, :] = features
            else:
                all_features[indices.numpy(), :] = features

    queries_features = all_features[eval_ds.database_num:]
    database_features = all_features[: eval_ds.database_num]
    logging.info(f"Final feature dim: {queries_features.shape[1]}")
        
    del all_features

    logging.debug("Calculating recalls")
    if args.prior_location_threshold == -1:
        if args.use_faiss_gpu:
            torch.cuda.empty_cache()
            res = faiss.StandardGpuResources()
            faiss_index = faiss.GpuIndexFlatL2(res, args.features_dim)
        else:
            faiss_index = faiss.IndexFlatL2(args.features_dim)
        faiss_index.add(database_features)
        distances, predictions = faiss_index.search(
            queries_features, max(args.recall_values)
        )
        del database_features
    else:
        distances, predictions = [[] for i in range(len(queries_features))], [[] for i in range(len(queries_features))]
        hard_negatives_per_query = eval_ds.get_hard_negatives()
        for query_index in tqdm(range(len(predictions))):
            faiss_index = faiss.IndexFlatL2(args.features_dim)
            faiss_index.add(database_features[hard_negatives_per_query[query_index]])
            distances_single, local_predictions_single = faiss_index.search(
                np.expand_dims(queries_features[query_index], axis=0), max(args.recall_values)
                )
            # logging.debug(f"distances_single:{distances_single}")
            # logging.debug(f"predictions_single:{predictions_single}")
            distances[query_index] = distances_single
            predictions_single = hard_negatives_per_query[query_index][local_predictions_single]
            predictions[query_index] = predictions_single
        distances = np.concatenate(distances, axis=0)
        predictions = np.concatenate(predictions, axis=0)
        del database_features
    if test_method == "nearest_crop":
        distances = np.reshape(distances, (eval_ds.queries_num, 20 * 5))
        predictions = np.reshape(predictions, (eval_ds.queries_num, 20 * 5))
        for q in range(eval_ds.queries_num):
            # sort predictions by distance
            sort_idx = np.argsort(distances[q])
            predictions[q] = predictions[q, sort_idx]
            # remove duplicated predictions, i.e. keep only the closest ones
            _, unique_idx = np.unique(predictions[q], return_index=True)
            # unique_idx is sorted based on the unique values, sort it again
            predictions[q, :20] = predictions[q, np.sort(unique_idx)][:20]
        predictions = predictions[
            :, :20
        ]  # keep only the closer 20 predictions for each query
    elif test_method == "maj_voting":
        distances = np.reshape(distances, (eval_ds.queries_num, 5, 20))
        predictions = np.reshape(predictions, (eval_ds.queries_num, 5, 20))
        for q in range(eval_ds.queries_num):
            # votings, modify distances in-place
            top_n_voting("top1", predictions[q],
                         distances[q], args.majority_weight)
            top_n_voting("top5", predictions[q],
                         distances[q], args.majority_weight)
            top_n_voting("top10", predictions[q],
                         distances[q], args.majority_weight)

            # flatten dist and preds from 5, 20 -> 20*5
            # and then proceed as usual to keep only first 20
            dists = distances[q].flatten()
            preds = predictions[q].flatten()

            # sort predictions by distance
            sort_idx = np.argsort(dists)
            preds = preds[sort_idx]
            # remove duplicated predictions, i.e. keep only the closest ones
            _, unique_idx = np.unique(preds, return_index=True)
            # unique_idx is sorted based on the unique values, sort it again
            # here the row corresponding to the first crop is used as a
            # 'buffer' for each query, and in the end the dimension
            # relative to crops is eliminated
            predictions[q, 0, :20] = preds[np.sort(unique_idx)][:20]
        predictions = predictions[
            :, 0, :20
        ]  # keep only the closer 20 predictions for each query

    # For each query, check if the predictions are correct
    positives_per_query = eval_ds.get_positives()
    # args.recall_values by default is [1, 5, 10, 20]
    recalls = np.zeros(len(args.recall_values))
    for query_index, pred in enumerate(predictions):
        for i, n in enumerate(args.recall_values):
            if np.any(np.in1d(pred[:n], positives_per_query[query_index])):
                recalls[i:] += 1
                break
    # Divide by the number of queries*100, so the recalls are in percentages
    recalls = recalls / eval_ds.queries_num * 100
    recalls_str = ", ".join(
        [f"R@{val}: {rec:.1f}" for val,
            rec in zip(args.recall_values, recalls)]
    )

    if args.use_best_n > 0:
        samples_to_be_used = args.use_best_n
        error_m = []
        position_m = []
        for query_index in range(len(predictions)):
            distance = distances[query_index]
            prediction = predictions[query_index]
            sort_idx = np.argsort(distance)
            if args.use_best_n == 1:
                best_position = eval_ds.database_utms[prediction[sort_idx[0]]]
            else:
                if distance[sort_idx[0]] == 0:
                    best_position = eval_ds.database_utms[prediction[sort_idx[0]]]
                else:
                    mean = distance[sort_idx[0]]
                    sigma = distance[sort_idx[0]] / distance[sort_idx[-1]]
                    X = np.array(distance[sort_idx[:samples_to_be_used]]).reshape((-1,))
                    weights = np.exp(-np.square(X - mean) / (2 * sigma ** 2))  # gauss
                    weights = weights / np.sum(weights)

                    x = y = 0
                    for p, w in zip(eval_ds.database_utms[prediction[sort_idx[:samples_to_be_used]]], weights.tolist()):
                        y += p[0] * w
                        x += p[1] * w
                    best_position = (y, x)
            actual_position = eval_ds.queries_utms[query_index]
            error = np.linalg.norm((actual_position[0]-best_position[0], actual_position[1]-best_position[1]))
            error_m.append(error)
            position_m.append(actual_position)
        process_results_simulation(error_m, args.save_dir)
        dataset_name_list = args.dataset_name.split('_')
        database_name = dataset_name_list[0]
        database_index = int(dataset_name_list[1])
        queries_name = dataset_name_list[2]
        if len(dataset_name_list[3]) > 1:
            queries_index = []
            for i in range(len(dataset_name_list[3])):
                queries_index.append(int(dataset_name_list[3][i]))
        else:
            queries_index = int(dataset_name_list[3])
        folder_config_path = './folder_config.yml'
        with open(folder_config_path, 'r') as f:
            folder_config = yaml.safe_load(f)
        database_image_path = os.path.join(args.datasets_folder, folder_config[database_name]['name'],
                                            folder_config[database_name]['maps'][database_index])
        database_region = folder_config[database_name]['valid_regions'][database_index]
        if hasattr(queries_index, "__len__"):
            for i in range(len(queries_index)):
                queries_index_single = queries_index[i]
                queries_region = folder_config[queries_name]['valid_regions'][queries_index_single]
                valid_region = calc_overlap(database_region, queries_region)
                save_heatmap_simulation(position_m, error_m, database_image_path, valid_region, args.save_dir, i)
        else:
            queries_region = folder_config[queries_name]['valid_regions'][queries_index]
            valid_region = calc_overlap(database_region, queries_region)
            save_heatmap_simulation(position_m, error_m, database_image_path, valid_region, args.save_dir)
            
    return recalls, recalls_str

def test_translation(args, eval_ds, model):
    """Compute PSNR of the given dataset and compute the recalls."""

    model = model.eval()
    psnr_sum = 0
    psnr_count = 0
    with torch.no_grad():
        # For database use "hard_resize", although it usually has no effect because database images have same resolution
        eval_ds.test_method = "hard_resize"

        eval_ds.is_inference = True
        eval_ds.compute_pairs(args)
        eval_ds.is_inference = False

        eval_dataloader = DataLoader(
            dataset=eval_ds,
            num_workers=args.num_workers,
            batch_size=1,
            collate_fn=datasets_ws.collate_fn,
            pin_memory=(args.device == "cuda"),
        )

        logging.debug("Calculating PSNR")

        for images, pairs_local_indexes, _ in tqdm(eval_dataloader, ncols=100):
            # Compute features of all images (images contains queries, positives and negatives)
            query_images_index = np.arange(0, len(images), 1 + 1)
            images_index = np.arange(0, len(images))
            database_images_index = np.setdiff1d(images_index, query_images_index, assume_unique=True)
            query_images = images[query_images_index] * 0.5 + 0.5
            database_images = images[database_images_index]
            output_images = model(database_images.to(args.device)).cpu()
            output_images = torch.clip(output_images, min=-1, max=1) * 0.5 + 0.5
            psnr_sum += calculate_psnr(query_images, output_images)
            psnr_count += 1

    psnr_sum /= psnr_count

    psnr_str = f"PSNR: {psnr_sum:.1f}"
            
    return psnr_sum, psnr_str

def top_n_voting(topn, predictions, distances, maj_weight):
    if topn == "top1":
        n = 1
        selected = 0
    elif topn == "top5":
        n = 5
        selected = slice(0, 5)
    elif topn == "top10":
        n = 10
        selected = slice(0, 10)
    # find predictions that repeat in the first, first five,
    # or fist ten columns for each crop
    vals, counts = np.unique(predictions[:, selected], return_counts=True)
    # for each prediction that repeats more than once,
    # subtract from its score
    for val, count in zip(vals[counts > 1], counts[counts > 1]):
        mask = predictions[:, selected] == val
        distances[:, selected][mask] -= maj_weight * count / n
