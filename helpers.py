from typing import Dict, List, Union

import numpy as np
import pandas as pd
import torch
from Bio.PDB.Polypeptide import index_to_one, one_to_index
from torch.nn.functional import softmax
from torch.utils.data import DataLoader, Dataset

from cavity_model import (
    CavityModel,
    ResidueEnvironmentsDataset,
    ToTensor,
)


def _train_val_split(
    parsed_pdb_filenames: List[str],
    TRAIN_VAL_SPLIT: float,
    DEVICE: str,
    BATCH_SIZE: int,
):
    """
    Helper function to perform training and validation split of ResidueEnvironments. Note that
    we do the split on PDB level not on ResidueEnvironment level due to possible leakage.
    """
    n_train_pdbs = int(len(parsed_pdb_filenames) * TRAIN_VAL_SPLIT)
    filenames_train = parsed_pdb_filenames[:n_train_pdbs]
    filenames_val = parsed_pdb_filenames[n_train_pdbs:]

    to_tensor_transformer = ToTensor(DEVICE)

    dataset_train = ResidueEnvironmentsDataset(
        filenames_train, transformer=to_tensor_transformer
    )
    dataset_val = ResidueEnvironmentsDataset(
        filenames_val, transformer=to_tensor_transformer
    )

    dataloader_train = DataLoader(
        dataset_train,
        batch_size=BATCH_SIZE,
        shuffle=True,
        collate_fn=to_tensor_transformer.collate_cat,
        drop_last=True,
    )
    # TODO: Fix it so drop_last doesn't have to be True when calculating validation accuracy.
    dataloader_val = DataLoader(
        dataset_val,
        batch_size=BATCH_SIZE,
        shuffle=False,
        collate_fn=to_tensor_transformer.collate_cat,
        drop_last=True,
    )

    print(
        f"Training data set includes {len(filenames_train)} pdbs with "
        f"{len(dataset_train)} environments."
    )
    print(
        f"Validation data set includes {len(filenames_val)} pdbs with "
        f"{len(dataset_val)} environments."
    )

    return dataloader_train, dataset_train, dataloader_val, dataset_val


def _train_step(
    batch_x: torch.Tensor,
    batch_y: torch.Tensor,
    cavity_model_net: CavityModel,
    optimizer: torch.optim.Adam,
    loss_function: torch.nn.CrossEntropyLoss,
) -> (torch.Tensor, float):
    """
    Helper function to take a training step
    """
    cavity_model_net.train()
    optimizer.zero_grad()
    batch_y_pred = cavity_model_net(batch_x)
    loss_batch = loss_function(batch_y_pred, torch.argmax(batch_y, dim=-1))
    loss_batch.backward()
    optimizer.step()
    return (batch_y_pred, loss_batch.detach().cpu().item())


def _eval_loop(
    cavity_model_net: CavityModel,
    dataloader_val: DataLoader,
    loss_function: torch.nn.CrossEntropyLoss,
) -> (float, float):
    """
    Helper function to perform an eval loop
    """
    # Eval loop. Due to memory, we don't pass the whole eval set to the model
    labels_true_val = []
    labels_pred_val = []
    loss_batch_list_val = []
    for batch_x_val, batch_y_val in dataloader_val:
        cavity_model_net.eval()
        batch_y_pred_val = cavity_model_net(batch_x_val)

        loss_batch_val = loss_function(
            batch_y_pred_val, torch.argmax(batch_y_val, dim=-1)
        )
        loss_batch_list_val.append(loss_batch_val.detach().cpu().item())

        labels_true_val.append(torch.argmax(batch_y_val, dim=-1).detach().cpu().numpy())
        labels_pred_val.append(
            torch.argmax(batch_y_pred_val, dim=-1).detach().cpu().numpy()
        )
    acc_val = np.mean(
        (np.reshape(labels_true_val, -1) == np.reshape(labels_pred_val, -1))
    )
    loss_val = np.mean(loss_batch_list_val)
    return acc_val, loss_val


def _train_loop(
    dataloader_train: DataLoader,
    dataloader_val: DataLoader,
    cavity_model_net: CavityModel,
    loss_function: torch.nn.CrossEntropyLoss,
    optimizer: torch.optim.Adam,
    EPOCHS: int,
    PATIENCE_CUTOFF: int,
):
    current_best_epoch_idx = -1
    current_best_loss_val = 1e4
    patience = 0
    epoch_idx_to_model_path = {}
    for epoch in range(EPOCHS):
        labels_true = []
        labels_pred = []
        loss_batch_list = []
        for batch_x, batch_y in dataloader_train:
            # Take train step
            batch_y_pred, loss_batch = _train_step(
                batch_x, batch_y, cavity_model_net, optimizer, loss_function
            )
            loss_batch_list.append(loss_batch)

            labels_true.append(torch.argmax(batch_y, dim=-1).detach().cpu().numpy())
            labels_pred.append(
                torch.argmax(batch_y_pred, dim=-1).detach().cpu().numpy()
            )

        # Train epoch metrics
        acc_train = np.mean(
            (np.reshape(labels_true, -1) == np.reshape(labels_pred, -1))
        )
        loss_train = np.mean(loss_batch_list)

        # Validation epoch metrics
        acc_val, loss_val = _eval_loop(cavity_model_net, dataloader_val, loss_function)

        print(
            f"Epoch {epoch:2d}. Train loss: {loss_train:5.3f}. "
            f"Train Acc: {acc_train:4.2f}. Val loss: {loss_val:5.3f}. "
            f"Val Acc {acc_val:4.2f}"
        )

        # Save model
        model_path = f"cavity_models/model_epoch_{epoch:02d}.pt"
        epoch_idx_to_model_path[epoch] = model_path
        torch.save(cavity_model_net.state_dict(), model_path)

        # Early stopping
        if loss_val < current_best_loss_val:
            current_best_loss_val = loss_val
            current_best_epoch_idx = epoch
            patience = 0
        else:
            patience += 1
        if patience > PATIENCE_CUTOFF:
            print(f"Early stopping activated.")
            break

    best_model_path = epoch_idx_to_model_path[current_best_epoch_idx]
    print(
        f"Best epoch idx: {current_best_epoch_idx} with validation loss: "
        f"{current_best_loss_val:5.3f} and model_path: "
        f"{best_model_path}"
    )
    return best_model_path


def _populate_dfs_with_resenvs(
    ddg_data_dict: Dict[str, pd.DataFrame], resenv_datasets_look_up: Dict[str, Dataset]
):
    """
    Helper function populate ddG dfs with the WT ResidueEnvironment objects.
    """
    print(
        "Dropping data points where residue is not defined in structure "
        f"or due to missing parsed pdb file"
    )
    # Add wt residue environments to standard ddg data dataframes
    for ddg_data_key in ddg_data_dict.keys():
        resenvs_ddg_data = []
        for idx, row in ddg_data_dict[ddg_data_key].iterrows():
            resenv_key = (
                f"{row['pdbid']}{row['chainid']}_"
                f"{row['variant'][1:-1]}{row['variant'][0]}"
            )
            try:
                if "symmetric" in ddg_data_key:
                    ddg_data_key_adhoc_fix = "symmetric"
                else:
                    ddg_data_key_adhoc_fix = ddg_data_key
                resenv = resenv_datasets_look_up[ddg_data_key_adhoc_fix][resenv_key]
                resenvs_ddg_data.append(resenv)
            except KeyError:
                resenvs_ddg_data.append(np.nan)
        ddg_data_dict[ddg_data_key]["resenv"] = resenvs_ddg_data
        n_datapoints_before = ddg_data_dict[ddg_data_key].shape[0]
        ddg_data_dict[ddg_data_key].dropna(inplace=True)
        n_datapoints_after = ddg_data_dict[ddg_data_key].shape[0]
        print(
            f"dropped {n_datapoints_before - n_datapoints_after:4d} / "
            f"{n_datapoints_before:4d} data points from dataset {ddg_data_key}"
        )

        # Add wt and mt idxs to df
        ddg_data_dict[ddg_data_key]["wt_idx"] = ddg_data_dict[ddg_data_key].apply(
            lambda row: one_to_index(row["variant"][0]), axis=1
        )
        ddg_data_dict[ddg_data_key]["mt_idx"] = ddg_data_dict[ddg_data_key].apply(
            lambda row: one_to_index(row["variant"][-1]), axis=1
        )


def _populate_dfs_with_nlls_and_nlfs(
    ddg_data_dict: Dict[str, pd.DataFrame],
    cavity_model_infer_net: CavityModel,
    DEVICE: str,
    BATCH_SIZE: int,
    EPS: float,
    display_n_rows: Union[None, int] = 2,
):
    """
    Helper function to populate ddG dfs with predicted negative-log-likelihoods and negative-log-frequencies
    """

    # Load PDB amino acid frequencies used to approximate unfolded states
    pdb_nlfs = -np.log(np.load("data/pdb_frequencies.npz")["frequencies"])

    # Add predicted Nlls and NLFs to ddG dataframes
    for ddg_data_key in ddg_data_dict.keys():
        df = ddg_data_dict[ddg_data_key]

        # Perform predictions on matched residue environments
        ddg_resenvs = list(df["resenv"].values)
        ddg_resenv_dataset = ResidueEnvironmentsDataset(
            ddg_resenvs, transformer=ToTensor(DEVICE)
        )

        # Define dataloader for resenvs matched to ddG data
        ddg_resenv_dataloader = DataLoader(
            ddg_resenv_dataset,
            batch_size=BATCH_SIZE,
            shuffle=False,
            collate_fn=ToTensor(DEVICE).collate_cat,
            drop_last=False,
        )

        # Infer NLLs
        pred_nlls = []
        for batch_x, _ in ddg_resenv_dataloader:
            batch_pred_nlls = (
                -torch.log(softmax(cavity_model_infer_net(batch_x), dim=-1) + EPS)
                .detach()
                .cpu()
                .numpy()
            )
            pred_nlls.append(batch_pred_nlls)
        pred_nlls_list = [row for row in np.vstack(pred_nlls)]

        # Add NLLs to dataframe
        df["nlls"] = pred_nlls_list

        # Isolate WT and MT NLLs and add to datafra
        df["wt_nll"] = df.apply(lambda row: row["nlls"][row["wt_idx"]], axis=1)
        df["mt_nll"] = df.apply(lambda row: row["nlls"][row["mt_idx"]], axis=1)

        # Add PDB database statistics negative log frequencies to df
        df["wt_nlf"] = df.apply(lambda row: pdb_nlfs[row["wt_idx"]], axis=1)
        df["mt_nlf"] = df.apply(lambda row: pdb_nlfs[row["mt_idx"]], axis=1)

        # Add ddG prediction (without downstream model)
        df["ddg_pred_no_ds"] = df.apply(
            lambda row: row["mt_nll"] - row["mt_nlf"] - row["wt_nll"] + row["wt_nlf"],
            axis=1,
        )

        if display_n_rows:
            print(ddg_data_key)
            display(ddg_data_dict[ddg_data_key].head(display_n_rows))