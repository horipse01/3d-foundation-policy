# FP3: A 3D Foundation Policy for Robotic Manipulation

Rujia Yang<sup>1*</sup>, Geng Chen<sup>2,3*</sup>, Chuan Wen<sup>1,2,4‡</sup>, Yang Gao<sup>1,2,4‡</sup>

<sup>1</sup>IIIS, Tsinghua University &ensp; <sup>2</sup>Shanghai AI Laboratory &ensp; <sup>3</sup>UC San Diego &ensp; <sup>4</sup>Shanghai Qi Zhi Institute

<sup>*</sup>Equal contribution &ensp; <sup>‡</sup>Equal advising

[**[Homepage]**](https://3d-foundation-policy.github.io/) &ensp; [**[Paper]**](https://arxiv.org/pdf/2503.08950)

> 🏆 **ICRA 2026 Best Paper Award Finalist on Robot Learning**

<video src="https://3d-foundation-policy.github.io/static/images/fp3_video.mp4" controls width="100%"></video>

![](assets/concept.jpg)

 **3D Foundation Policy (FP3)** is a 1.3B 3D point cloud-based language-visuomotor policy pre-trained on 60k episodes from the DROID dataset. FP3 supports data-efficient fine-tuning for downstream tasks, while demonstrating superior generalizability to unseen environments and novel objects.


## Updates

- **[2025-06-05]** Weights of FP3 pre-trained on DROID release.
- **[2025-06-05]** Added support for DP2 and DP3 configuration generation for training Diffusion Policy and 3D Diffusion Policy.

---------
## Installation

See [INSTALL.md](INSTALL.md) for details.

## Workflow

### 1. Data Collection
- Calibrate cameras using the Droid GUI, following the guide here: [Calibrating Cameras](https://droid-dataset.github.io/droid/example-workflows/calibrating-cameras.html). Since we use point clouds as input, ensuring proper camera calibration is crucial.
- Collect data using the Droid GUI, following the instructions here: [Data Collection](https://droid-dataset.github.io/droid/example-workflows/data-collection.html).

### 2. Data processing
- The collected image information is stored in `.svo` files. We need to further process and combine this information into `.h5` files. Navigate to the project folder and run the following command:
  ```bash
  python droid_policy_learning/robomimic/scripts/conversion/convert_droid.py --folder <your_data_folder>
  ```

- After that, run this command to gather all your data and create a `dataset.json` file:
  ```bash
  python droid_policy_learning/robomimic/scripts/conversion/set_manifest_file.py --folder <your_data_folder> --lang <language_insruction>
  ```

- With `dataset.json`, we can then encode the language with the pretrained encoder and merge the feature into `.h5` files. Run the following command:
  ```bash
  python droid_policy_learning/robomimic/scripts/conversion/add_lang_to_converted_data.py --manifest_file droid_policy_learning/dataset.json
  ```

### 3. Download Pre-trained Weights
- You can download the checkpoint pre-trained on DROID by this link: [checkpoint on DROID](https://drive.google.com/file/d/1qSSgSiEq4JkHB4IzbuSdNxvY1QNSFHkT/view?usp=sharing). It can be used for finetuning or zero-shot evaluation. 

### 4. Model Training
- Our training scripts using deepspeed to accelerate training, so you need to configure it properly.

- Add language embedding to dataset by running:
  ```bash
  python droid_policy_learning/robomimic/scripts/conversion/add_lang_to_converted_data.py --manifest_file droid_policy_learning/dataset.json
  ```

- Generate config by running:
  ```bash
  python droid_policy_learning/robomimic/scripts/generate_config.py --dataset <your manifest file> --exp <experiment name>
  ```
  If you wish to train Diffusion Policy or 3D Diffusion Policy, you can generate configuration files for DP2 and DP3 in a similar manner.
  ```bash
  python droid_policy_learning/robomimic/scripts/generate_config_dp2.py --dataset <your manifest file> --exp <experiment name>
  ```
  ```bash
  python droid_policy_learning/robomimic/scripts/generate_config_dp3.py --dataset <your manifest file> --exp <experiment name>
  ```
  Once the configuration files are created, the subsequent training and evaluation procedures are identical to those of FP3.



- And then run:
  ```bash
  accelerate launch droid_policy_learning/robomimic/scripts/train.py --config <config path> --ckpt <ckpt_path>
  ``` 

  If you want to finetune on our pre-trained model, just download the checkpoint and specify the `ckpt` argument. 

### 5. Evaluation
- To evaluate a policy on a robot, you can just run:
  ```bash
  cat <your_language_instruction> > ./eval_params/lang_command.txt
  python droid/scripts/evaluation/evaluate_policy.py
  ```

---------

## Acknowledgment
Our code is generally built upon [DROID](https://github.com/droid-dataset/droid), [Robomimic](https://github.com/ARISE-Initiative/robomimic). We sincerely appreciate their contribution to the open-source community, which have significantly supported this project.  
