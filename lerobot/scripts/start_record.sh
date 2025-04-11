#!/bin/bash

# 设置USB设备权限
sudo chmod 777 /dev/ttyUSB0
sudo chmod 777 /dev/ttyUSB1
echo ""

# 获取用户输入

read -p "请输入机器人类型 (支持机器人: adora_dual、realman, 默认: adora_dual): " robot_type
robot_type=${robot_type:-adora_dual}
echo ""

read -p "请输入单任务描述 (英文输入): " single_task
single_task=${single_task:-"Grasp things"}
echo ""

read -p "请输入任务ID (默认: grasp): " task_id
task_id=${task_id:-"grasp"}
repo_id="${robot_type}/${task_id}"
echo "仓库ID: $repo_id"
echo ""

read -p "请输入要采集的条数 (默认: 30): " num_episodes
num_episodes=${num_episodes:-30}
echo ""

read -p "是否从上次记录继续? (true或false, 默认: false): " resume
resume=${resume:-false}
echo ""

# 运行Python脚本
python lerobot/scripts/control_robot.py \
  --robot.type="$robot_type" \
  --control.type=record \
  --control.single_task="$single_task" \
  --control.fps=30 \
  --control.repo_id="$repo_id" \
  --control.tags='["tutorial"]' \
  --control.warmup_time_s=240 \
  --control.episode_time_s=240 \
  --control.reset_time_s=240 \
  --control.num_episodes="$num_episodes" \
  --control.push_to_hub=false \
  --control.resume="$resume"