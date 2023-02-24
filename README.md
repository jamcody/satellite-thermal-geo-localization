# satellite-thermal-geo-localization

This is the official repository for Long-range UAV Thermal Geo-localization with Satellite Imagery.

Dataset link: [Google drive](https://drive.google.com/drive/folders/1sxkN1S3tvRmnP4C01Qqc2c8pWOsulPEG).

The ``dataset`` folder should be created in the root folder with the following structure

```
datasets/satellite_0_thermalmapping_135/
├── test_database.h5
├── test_queries.h5
├── train_database.h5
├── train_queries.h5
├── extended_database.h5
├── extended_queries.h5 # Generated by TGM
├── val_database.h5
└── val_queries.h5
```

Check your md5sum after downloading the dataset:
```
3d2faea188995dd687c77178621b6fc7  extended_database.h5
7ef56acc4a746e7bafe1df91131d7737  test_database.h5
f891d9feca5f4252169a811e8c8e648d  test_queries.h5
f075a7a6db5d5d61be88d252f7d6e05b  train_database.h5
b44ee39173bf356b24690ed6933a6792  train_queries.h5
31923c28dd074ddaacf0c463681f7d2b  val_database.h5
fdcb2e12d9a29b8d20a4cbd88bfe430c  val_queries.h5
```

You can find the training scripts and evaluation scripts in ``scripts`` folder. The scripts is for slurm system to submit sbatch job. If you want to run bash command, change the suffix from ``sbatch`` to ``sh`` and run with bash.

Note that running evaluation scripts requires two arguments: ```model_folder_name``` and ```backbone_name```. An example command is:
```
sbatch ./scripts/eval_contrast.sbatch  satellite_0_thermalmapping_135-2023-02-18_05-18-21-c0c572c4-a48a-4d5b-a152-bbc51b9161aa resnet18conv4
```
Or the bash command
```
./scripts/eval_contrast.sh  satellite_0_thermalmapping_135-2023-02-18_05-18-21-c0c572c4-a48a-4d5b-a152-bbc51b9161aa resnet18conv4
```

Running training scripts does not require arguments.
