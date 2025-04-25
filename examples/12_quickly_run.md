# 遥操

```
python lerobot/scripts/control_robot.py \
  --robot.type=adora_dual \
  --control.type=teleoperate
```

# 采集


make sure you've logged in using a write-access token


```
huggingface-cli login --token ${HUGGINGFACE_TOKEN} --add-to-git-credential
```

confirm the `--robot.type` before start

```
python lerobot/scripts/control_robot.py \
  --robot.type=adora_dual \
  --control.type=record \
  --control.single_task="Grasp a orange and put it in the bowl." \
  --control.fps=10 \
  --control.repo_id=Ryu-Yang/adora_dual_grasp \
  --control.tags='["tutorial"]' \
  --control.warmup_time_s=60 \
  --control.episode_time_s=60 \
  --control.reset_time_s=60 \
  --control.num_episodes=27 \
  --control.push_to_hub=true \
  --control.resume=true

```
  --control.resume=true
Checkpoints are done during recording, so if any issue occurs, you can resume recording by re-running the same command again with `--control.resume=true`. You will need to manually delete the dataset directory if you want to start recording from scratch.

# 查看

  --root /home/kemove/autodl-tmp/Ryu-Yang/adora_dual_stacking_cloth
```
python lerobot/scripts/visualize_dataset_html.py \
  --repo-id Ryu-Yang/adora_dual_stacking_cloth
```

# 训练

```\
export HF_LEROBOT_HOME=~/autodl-tmp/
python lerobot/scripts/train.py \
  --dataset.repo_id=Ryu-Yang/adora_dual_stacking_cloth \
  --policy.type=pi0 \
  --output_dir=outputs/train/pi0_adora_dual_stacking_cloth \
  --job_name=pi0_adora_dual_stacking_cloth \
  --policy.device=cuda \
  --wandb.enable=true
```

# 推理
```
python lerobot/scripts/control_robot.py \
  --robot.type=adora_dual \
  --control.type=record \
  --control.fps=10 \
  --control.single_task="Grasp a orange and put it in the bowl." \
  --control.repo_id=Ryu-Yang/eval_act_adora_dual_grasp \
  --control.tags='["tutorial"]' \
  --control.warmup_time_s=60 \
  --control.episode_time_s=60 \
  --control.reset_time_s=60 \
  --control.num_episodes=5 \
  --control.push_to_hub=true \
  --control.policy.path=outputs/train/act_adora_dual_grasp/checkpoints/last/pretrained_model
```
