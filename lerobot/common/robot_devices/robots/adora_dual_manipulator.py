import pickle
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
import os
import ctypes
import platform
import sys

import numpy as np
import torch

from lerobot.common.robot_devices.cameras.utils import Camera
from lerobot.common.robot_devices.motors.dynamixel import (
    DriveMode,
    DynamixelMotorsBus,
    OperatingMode,
    TorqueMode,
)

from lerobot.common.robot_devices.cameras.utils import make_cameras_from_configs
from lerobot.common.robot_devices.motors.utils import MotorsBus, make_motors_buses_from_configs
from lerobot.common.robot_devices.utils import RobotDeviceAlreadyConnectedError, RobotDeviceNotConnectedError
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from lerobot.common.robot_devices.robots.configs import AdoraDualRobotConfig
from functools import cache
import traceback
import logging
from threading import Thread
import threading


URL_HORIZONTAL_POSITION = {
    "follower": "https://raw.githubusercontent.com/huggingface/lerobot/main/media/koch/follower_horizontal.png",
    "leader": "https://raw.githubusercontent.com/huggingface/lerobot/main/media/koch/leader_horizontal.png",
}
URL_90_DEGREE_POSITION = {
    "follower": "https://raw.githubusercontent.com/huggingface/lerobot/main/media/koch/follower_90_degree.png",
    "leader": "https://raw.githubusercontent.com/huggingface/lerobot/main/media/koch/leader_90_degree.png",
}

########################################################################
# Calibration logic
########################################################################


TARGET_HORIZONTAL_POSITION = np.array([0, 0, 0, 0, 0, 0, 0, 0])
TARGET_90_DEGREE_POSITION = np.array([0, 0, 0, 0, 0, 0, 0, 0])
GRIPPER_OPEN = np.array([600])

FLAG_FOLLOW=0 #1为高跟随,0为低跟随

WINDOW_SIZE = 1

# 常量预计算
SCALE_FACTOR = 90 / 1024  # 电机值到关节角度的比例系数
GRIPPER_SCALE = 100 / 600  # 夹爪转换比例系数
# GRIPPER_OFFSET = (98 * 100) / 62          # 夹爪转换偏移量

DLL_PATH = os.getenv("DLL_PATH", "lerobot/common/robot_devices/robots/libs")

# End loader
if platform.machine() == "x86_64":
    DLL_PATH = os.path.join(DLL_PATH, 'linux_x86', 'libRM_Base.so.1.0.0')
elif sys.platform == "win32":
    if sys.maxsize > 2**32:
        DLL_PATH = os.path.join(DLL_PATH, 'win_64', 'RM_Base.dll')
    else:
        DLL_PATH = os.path.join(DLL_PATH, 'win_32', 'RM_Base.dll')
        print(DLL_PATH)
else:
    DLL_PATH = os.path.join(DLL_PATH, 'linux_arm', 'libRM_Base.so.1.0.0')

@cache
def is_headless():
    """Detects if python is running without a monitor."""
    try:
        import pynput  # noqa

        return False
    except Exception:
        print(
            "Error trying to import pynput. Switching to headless mode. "
            "As a result, the video stream from the cameras won't be shown, "
            "and you won't be able to change the control flow with keyboards. "
            "For more info, see traceback below.\n"
        )
        traceback.print_exc()
        print()
        return True

def init_keyboard_listener():
    # Allow to exit early while recording an episode or resetting the environment,
    # by tapping the right arrow key '->'. This might require a sudo permission
    # to allow your terminal to monitor keyboard events.
    events = {}
    events["complete"] = False

    if is_headless():
        logging.warning(
            "Headless environment detected. On-screen cameras display and keyboard inputs will not be available."
        )
        listener = None
        return listener, events

    # Only import pynput if not in a headless environment
    from pynput import keyboard

    def on_press(key):
        try:
            hasattr(key, 'char')
            if key.char.lower() == 'q':
                print("key pressed")
                events["complete"] = True
        except Exception as e:
            print(f"Error handling key press: {e}")

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    return listener, events

def apply_homing_offset(values: np.array, homing_offset: np.array) -> np.array:
    for i in range(len(values)):
        if values[i] is not None:
            values[i] += homing_offset[i]
    return values


def apply_drive_mode(values: np.array, drive_mode: np.array) -> np.array:
    for i in range(len(values)):
        if values[i] is not None and drive_mode[i]:
            values[i] = -values[i]
    return values


def apply_calibration(values: np.array, homing_offset: np.array, drive_mode: np.array) -> np.array:
    values = apply_drive_mode(values, drive_mode)
    values = apply_homing_offset(values, homing_offset)
    return values


def revert_calibration(values: np.array, homing_offset: np.array, drive_mode: np.array) -> np.array:
    """
    Transform working position into real position for the robot.
    """
    values = apply_homing_offset(
        values,
        np.array([-homing_offset if homing_offset is not None else None for homing_offset in homing_offset]),
    )
    values = apply_drive_mode(values, drive_mode)
    return values


def revert_appropriate_positions(positions: np.array, drive_mode: list[bool]) -> np.array:
    for i, revert in enumerate(drive_mode):
        if not revert and positions[i] is not None:
            positions[i] = -positions[i]
    return positions


def compute_corrections(positions: np.array, drive_mode: list[bool], target_position: np.array) -> np.array:
    correction = revert_appropriate_positions(positions, drive_mode)

    for i in range(len(positions)):
        if correction[i] is not None:
            if drive_mode[i]:
                correction[i] -= target_position[i]
            else:
                correction[i] += target_position[i]

    return correction


def compute_nearest_rounded_positions(positions: np.array) -> np.array:
    return np.array(
        [
            round(positions[i] / 1024) * 1024 if positions[i] is not None else None
            for i in range(len(positions))
        ]
    )


#用于计算机械臂的归零偏差
def compute_homing_offset(
    arm: DynamixelMotorsBus, drive_mode: list[bool], target_position: np.array
) -> np.array:
    # Get the present positions of the servos
    #从 arm 读取当前伺服电机的位置（“Present_Position”）。apply_calibration 函数使用 drive_mode 
    # 和一个初始零位置数组对这些位置进行校准。
    present_positions = apply_calibration(
        arm.read("Present_Position"), np.array([0, 0, 0, 0, 0, 0, 0, 0]), drive_mode
    )

    nearest_positions = compute_nearest_rounded_positions(present_positions)
    correction = compute_corrections(nearest_positions, drive_mode, target_position)
    return correction


def compute_drive_mode(arm: DynamixelMotorsBus, offset: np.array):
    
    # Get current positions
    present_positions = apply_calibration(
        arm.read("Present_Position"), offset, np.array([False, False, False, False, False, False,False,False])
    )

    nearest_positions = compute_nearest_rounded_positions(present_positions)

    # construct 'drive_mode' list comparing nearest_positions and TARGET_90_DEGREE_POSITION
    drive_mode = []
    for i in range(len(nearest_positions)):
        drive_mode.append(nearest_positions[i] != TARGET_90_DEGREE_POSITION[i])
    return drive_mode

# 初始化电机
def init_arm(arm: MotorsBus):
    arm.write("Torque_Enable", TorqueMode.ENABLED.value)
    arm.write("Operating_Mode", OperatingMode.CURRENT_CONTROLLED_POSITION.value)
    arm.write("Goal_Current", 300)

    # Make sure the native calibration (homing offset abd drive mode) is disabled, since we use our own calibration layer to be more generic
    arm.write("Homing_Offset", 0)
    arm.write("Drive_Mode", DriveMode.NON_INVERTED.value)

    print("init_arm: OK!")

# 重置电机的状态，禁用扭矩模式，设置伺服电机的模式
def reset_arm(arm: MotorsBus):
    # To be configured, all servos must be in "torque disable" mode
    arm.write("Torque_Enable", TorqueMode.DISABLED.value)   # 将所有伺服电机的扭矩模式设置为禁用，以确保电机在重置过程中不会施加力量。

    # Use 'extended position mode' for all motors except gripper, because in joint mode the servos can't
    # rotate more than 360 degrees (from 0 to 4095) And some mistake can happen while assembling the arm,
    # you could end up with a servo with a position 0 or 4095 at a crucial point See [
    # https://emanual.robotis.com/docs/en/dxl/x/x_series/#operating-mode11]
    all_motors_except_gripper = [name for name in arm.motor_names if name != "gripper"]
    arm.write("Operating_Mode", OperatingMode.EXTENDED_POSITION.value, all_motors_except_gripper)

    # TODO(rcadene): why?
    # Use 'position control current based' for gripper
    arm.write("Operating_Mode", OperatingMode.CURRENT_CONTROLLED_POSITION.value, "gripper")
    arm.write("Goal_Current", 50, "gripper")

    # Set gripper
    arm.write("Torque_Enable", 1, "gripper")
    arm.write("Goal_Position", GRIPPER_OPEN, "gripper")

    print("reset_arm: OK!")


def run_arm_calibration(arm: MotorsBus, name: str, arm_type: str):
    """Example of usage:
    ```python
    run_arm_calibration(arm, "left", "follower")
    ```
    """
    #先重置机械臂
    init_arm(arm)
    
    listener, events = init_keyboard_listener()

    print(f"Calibrate {name} arm.")

    while True:
        current = arm.read("Present_Current")
        my_pos = arm.read("Present_Position")
        print(f"current = {current}")
        print(f"my_pos = {my_pos}")
        for index, value in enumerate(current, start=0):
            if value >> 15 == 1:
                buma = value  # 补码（这里实际上只是获取 value[0] 的值）
                fanma = buma - 1  # 反码（这里实际上是 value[0] 减 1）
                yuanma = int(format(~fanma & 0xFFFF, '016b'), 2)  # 原码（这里是对 fanma 进行按位取反操作）  
                p_current = yuanma * -1
            else:
                p_current = float(value)
            print(f"index = {index}, value = {value}, p_current = {p_current}")
            print(f"index = {index}, current[{index}] = {current[index]}")
            print(f"index = {index}, my_pos[{index}] = {my_pos[index]}")
            print(f"index = {index}, arm.motor_names[{index}] = {arm.motor_names[index]}")
            if abs(p_current) >= 150:
                arm.write("Goal_Position", my_pos[index], arm.motor_names[index])
        if events["complete"]:
            old_gripper = arm.read("Present_Position", "gripper")
            print(f"Will complete {name} arm calibration")
            while True:
                gripper = arm.read("Present_Position", "gripper")
                print(f"gripper = {gripper}")
                if abs(gripper - old_gripper) >= 150:
                    break
            break

    if not is_headless():
        if listener is not None:
            listener.stop()

    #提示用户放置到水平位置
    # TODO(rcadene): document what position 1 mean
    print(
        f"Please move the '{name} {arm_type}' arm to the horizontal position (gripper fully closed, see {URL_HORIZONTAL_POSITION[arm_type]})"
    )
    # input("Press Enter to continue...")
    #调用 compute_homing_offset 函数来计算在水平位置时的归零偏差，TARGET_HORIZONTAL_POSITION 是预设的水平目标位置。
    #TARGET_HORIZONTAL_POSITION 是预设的水平目标位置。
    horizontal_homing_offset = compute_homing_offset(
        arm, [False, False, False, False, False, False, False, False], TARGET_HORIZONTAL_POSITION
    )

    # TODO(rcadene): document what position 2 mean
    #显示一条消息，提示用户将机械臂移动到90度位置，并要求用户在调整后按回车继续。
    print(
        f"Please move the '{name} {arm_type}' arm to the 90 degree position (gripper fully open, see {URL_90_DEGREE_POSITION[arm_type]})"
    )
    # input("Press Enter to continue...")
    
    #计算驱动模式和归零偏差
    drive_mode = compute_drive_mode(arm, horizontal_homing_offset)
    homing_offset = compute_homing_offset(arm, drive_mode, TARGET_90_DEGREE_POSITION)

    # Invert offset for all drive_mode servos
    for i in range(len(drive_mode)):
        if drive_mode[i]:
            homing_offset[i] = -homing_offset[i]

    print("Calibration is done!")

    print("=====================================")
    print("      HOMING_OFFSET: ", " ".join([str(i) for i in homing_offset]))
    print("      DRIVE_MODE: ", " ".join([str(i) for i in drive_mode]))
    print("=====================================")

    return homing_offset, drive_mode


########################################################################
# Alexander Koch robot arm
########################################################################

class MovingAverageFilter:
    def __init__(self, window_size):
        self.window_size = window_size
        self.buffer = deque(maxlen=window_size)
    
    def update(self, new_value):
        self.buffer.append(new_value)
        return sum(self.buffer) / len(self.buffer)
    
    def get_last(self):
        return self.buffer[0]


class GEN72Arm:
    def __init__(self, ip, start_pose, joint_p_limit, joint_n_limit, fps):
        self.ip = ip
        self.start_pose = start_pose
        self.joint_p_limit = joint_p_limit
        self.joint_n_limit = joint_n_limit
        #  gen72机械臂接入，dll文件路径
        dllPath = DLL_PATH
        self.pDll = ctypes.cdll.LoadLibrary(dllPath)
        #  连接机械臂
        self.pDll.RM_API_Init(72,0)
        self.byteIP = bytes(ip, "gbk")
        self.nSocket = self.pDll.Arm_Socket_Start(self.byteIP, 8080, 200)
        print(f"self.nSocket = {self.nSocket}")
        #  夹爪标志位
        self.gipflag=1
        self.gipflag_send=1
        # self.gipvalue=80
        #远程操控部分-读取领导臂的目标位置
        # self.leader_pos = {}
        #远程操控部分-写入gen72 API的关节数据
        float_joint = ctypes.c_float*7
        self.joint_teleop_write = [0.0] * 7
        #远程操控部分-读取gen72 API的关节数据
        self.joint_teleop_read = float_joint()
        #数据观察部分-读取gen72 API的关节数据
        self.joint_obs_read=float_joint()
        #数据观察部分-上传关节以及夹爪开合度
        self.joint_obs_present=np.zeros(8, dtype=np.float32)
        #推理输出部分-接收模型推理的关节角度，写入gen72 API中
        self.joint_send=float_joint()


        self.joint_async_read = None

        self.old_grasp = 100

        self.clipped_gripper = 100
        self.fps = fps
        self.thread = None
        self.stop_event = None
        self.logs = {}

        self.filters = [MovingAverageFilter(WINDOW_SIZE) for _ in range(7)]

        #gen72API
        self.pDll.Movej_Cmd.argtypes = (ctypes.c_int, ctypes.c_float * 7, ctypes.c_byte, ctypes.c_float, ctypes.c_bool)
        self.pDll.Movej_Cmd.restype = ctypes.c_int
        self.pDll.Movej_CANFD.argtypes= (ctypes.c_int, ctypes.c_float * 7, ctypes.c_bool, ctypes.c_float)
        self.pDll.Movej_CANFD.restype = ctypes.c_int
        self.pDll.Get_Joint_Degree.argtypes = (ctypes.c_int, ctypes.c_float * 7)
        self.pDll.Get_Joint_Degree.restype = ctypes.c_int
        #self.pDll.Get_Gripper_State.argtypes = (ctypes.c_int, ctypes.POINTER(GripperState))
        self.pDll.Get_Gripper_State.restype = ctypes.c_int
        self.pDll.Set_Gripper_Position.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_int)
        self.pDll.Set_Gripper_Position.restype = ctypes.c_int
        self.pDll.Write_Single_Register.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,ctypes.c_bool)
        self.pDll.Write_Single_Register.restype = ctypes.c_int
        self.pDll.Set_Modbus_Mode.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_bool)
        self.pDll.Set_Modbus_Mode.restype = ctypes.c_int
        self.pDll.Set_Tool_Voltage.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_bool)
        self.pDll.Set_Tool_Voltage.restype = ctypes.c_int
        self.pDll.Close_Modbus_Mode.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_bool)
        self.pDll.Close_Modbus_Mode.restype = ctypes.c_int
        self.pDll.Get_Read_Holding_Registers.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
        self.pDll.Get_Read_Holding_Registers.restype=ctypes.c_int
        self.pDll.Set_High_Speed_Eth.argtypes = (ctypes.c_int, ctypes.c_byte, ctypes.c_bool)
        self.pDll.Set_High_Speed_Eth.restype = ctypes.c_int

        #打开高速网络配置
        self.pDll.Set_High_Speed_Eth(self.nSocket, 1, 0)
        #设置末端工具接口电压为24v
        self.pDll.Set_Tool_Voltage(self.nSocket, 3, 1)
        #打开modbus模式
        self.pDll.Set_Modbus_Mode(self.nSocket, 1, 115200, 2, 2, 1)
        #初始化夹爪为打开状态
        self.pDll.Write_Single_Register(self.nSocket, 1, 40000, 100, 1, 1)
        #配置碰撞检测等级为无检测
        self.pDll.Set_Collision_Stage(self.nSocket, 0, 0)

        self.is_connected = True
    
    def movej_cmd(self, joint):
        clipped_joint = max(self.joint_n_limit[:7], min(self.joint_p_limit[:7], joint[:7]))
        c_joint = (ctypes.c_float * 7)(*clipped_joint)
        self.pDll.Movej_Cmd(self.nSocket, c_joint, 20, 1, 0)
    
    def movej_canfd(self, joint):
        clipped_joint = max(self.joint_n_limit[:7], min(self.joint_p_limit[:7], joint[:7]))
        c_joint = (ctypes.c_float * 7)(*clipped_joint)
        self.pDll.Movej_CANFD(self.nSocket, c_joint, FLAG_FOLLOW, 0)

    def write_single_register(self, gripper):
        clipped_gripper = max(0, min(100, gripper))
        c_gripper = ctypes.c_int(clipped_gripper)
        self.pDll.Write_Single_Register(self.nSocket, 1, 40000, c_gripper, 1, 0)

    def read_joint_degree(self):
        self.pDll.Get_Joint_Degree(self.nSocket, self.joint_teleop_read)
        return self.joint_teleop_read
    
    def disconnect(self):
        self.pDll.Close_Modbus_Mode(self.nSocket, 1, 1)

        if self.thread is not None:
            self.stop_event.set()
            self.thread.join()  # wait for the thread to finish
            self.thread = None
            self.stop_event = None

        self.is_connected = False

    def read_loop(self):
        while not self.stop_event.is_set():
            try:
                self.joint_async_read = self.read_joint_degree()
            except Exception as e:
                print(f"Error reading in thread: {e}")

    def async_read_joint_degree(self):
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                f"GEN72Arm({self.byteIP}) is not connected. Try running `GEN72Arm()` first."
            )

        if self.thread is None:
            self.stop_event = threading.Event()
            self.thread = Thread(target=self.read_loop, args=())
            self.thread.daemon = True
            self.thread.start()

        num_tries = 0
        while True:
            if self.joint_async_read is not None:
                return self.joint_async_read

            time.sleep(1 / self.fps)
            num_tries += 1
            if num_tries > self.fps * 2:
                raise TimeoutError("Timed out waiting for async_read() to start.")

class AdoraDualManipulator:
    def __init__(self, config: AdoraDualRobotConfig):
        self.config = config
        self.robot_type = self.config.type

        self.calibration_path = {}
        self.calibration_path['right'] = Path(self.config.right_arm_config['calibration_dir'])
        self.calibration_path['left'] = Path(self.config.left_arm_config['calibration_dir'])

        self.leader_arms = {}
        self.leader_arms['right'] = DynamixelMotorsBus(self.config.right_leader_arm)
        self.leader_arms['left'] = DynamixelMotorsBus(self.config.left_leader_arm)
        self.cameras = make_cameras_from_configs(self.config.cameras)

        self.follower_arms = {}
        self.follower_arms['right'] = GEN72Arm(
            ip = self.config.right_arm_config['ip'],
            start_pose = self.config.right_arm_config['start_pose'],
            joint_p_limit = self.config.right_arm_config['joint_p_limit'],
            joint_n_limit = self.config.right_arm_config['joint_n_limit'],
            fps = self.config.right_arm_config['fps'],
        )
        self.follower_arms['left'] = GEN72Arm(
            ip = self.config.left_arm_config['ip'],
            start_pose = self.config.left_arm_config['start_pose'],
            joint_p_limit = self.config.left_arm_config['joint_p_limit'],
            joint_n_limit = self.config.left_arm_config['joint_n_limit'],
            fps = self.config.left_arm_config['fps'],
        )
        
        self.is_connected = False
        self.logs = {}
        self.frame_counter = 0  # 帧计数器

        

    def get_motor_names(self, arm: dict[str, MotorsBus]) -> list:
        return [f"{arm}_{motor}" for arm, bus in arm.items() for motor in bus.motors]

    @property
    def camera_features(self) -> dict:
        cam_ft = {}
        for cam_key, cam in self.cameras.items():
            key = f"observation.images.{cam_key}"
            cam_ft[key] = {
                "shape": (cam.height, cam.width, cam.channels),
                "names": ["height", "width", "channels"],
                "info": None,
            }
        return cam_ft
    
    @property
    def motor_features(self) -> dict:
        action_names = self.get_motor_names(self.leader_arms)
        state_names = self.get_motor_names(self.leader_arms)
        return {
            "action": {
                "dtype": "float32",
                "shape": (len(action_names),),
                "names": action_names,
            },
            "observation.state": {
                "dtype": "float32",
                "shape": (len(state_names),),
                "names": state_names,
            },
        }
    
    @property
    def features(self):
        return {**self.motor_features, **self.camera_features}

    @property
    def has_camera(self):
        return len(self.cameras) > 0

    @property
    def num_cameras(self):
        return len(self.cameras)

    def connect(self):
        if self.is_connected:
            raise RobotDeviceAlreadyConnectedError(
                "KochRobot is already connected. Do not run `robot.connect()` twice."
            )
        if not self.leader_arms and not self.cameras:
            raise ValueError(
                "KochRobot doesn't have any device to connect. See example of usage in docstring of the class."
            )
        
        for name in self.leader_arms:
            # Connect the arms
            print(f"test printf : {name} leader arm.")
            print(f"test printf ip : {self.follower_arms[name].ip} ")
            print(f"test printf calibration_path : {self.calibration_path[name]} ")
            print(f"test printf port : {self.leader_arms[name].port} ")

        for name in self.leader_arms:
            # Connect the arms
            print(f"Connecting {name} leader arm.")
            self.leader_arms[name].connect()

            # # Reset the arms and load or run calibration
            # if self.calibration_path[name].exists():

            #     reset_arm(self.leader_arms[name])

            #     with open(self.calibration_path[name], "rb") as f:
            #         calibration = pickle.load(f)

            #     #gen72关节初始化，移动到 初始位置
            #     ret=self.follower_arms[name].movej_cmd(self.follower_arms[name].start_pose)
            #     print('机械臂回到 初始位置 ',ret)
            # else:
                # Run calibration process which begins by reseting all arms
            print(f"run_calibration {name} leader arm.")
            calibration = self.run_calibration(name)
            print(f"end run_calibration {name} leader arm.")

                # self.calibration_path[name].parent.mkdir(parents=True, exist_ok=True)
                # with open(self.calibration_path[name], "wb") as f:
                #     pickle.dump(calibration, f)

                #gen72关节初始化，移动到 零位
            ret=self.follower_arms[name].movej_cmd([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            print('机械臂回到 零位 ',ret)
            
            print(f"{name} leader arm :  set calib and gripper")
            # Set calibration
            self.leader_arms[name].set_calibration(calibration[f"leader_{name}"])


            print(f"Connect {name} leader arm Sucsses!")

        # Connect the cameras
        for name in self.cameras:
            self.cameras[name].connect()

        for name in self.leader_arms:
            reset_arm(self.leader_arms[name])


        self.is_connected = True

    def run_calibration(self, name):
        calibration = {}

        homing_offset, drive_mode = run_arm_calibration(self.leader_arms[name], name, "leader")

        print(f"run_arm_calibration OK")

        calibration[f"leader_{name}"] = {}
        for idx, motor_name in enumerate(self.leader_arms[name].motor_names):
            calibration[f"leader_{name}"][motor_name] = (homing_offset[idx], drive_mode[idx])

        return calibration


    def teleop_step(
        self, record_data=False, 
    ) -> None | tuple[dict[str, torch.Tensor], dict[str, torch.Tensor]]:
        self.frame_counter += 1

        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                "KochRobot is not connected. You need to run `robot.connect()`."
            )

        for name in self.leader_arms:
            # if name == "left":
            #     continue
            # 读取领导臂电机数据
            read_start = time.perf_counter()
            leader_pos = self.leader_arms[name].async_read("Present_Position")
            # self.follower_arms[name].leader_pos = leader_pos
            self.logs[f"read_leader_{name}_pos_dt_s"] = time.perf_counter() - read_start

            # 电机数据到关节角度的转换，关节角度处理（向量化操作）
            for i in range(7):
                # 数值转换
                value = round(leader_pos[i] * SCALE_FACTOR, 2)

                # 特定关节取反（3号和5号）
                if i in {3, 5}:
                    value = -value

                # 限幅
                clamped_value = max(self.follower_arms[name].joint_n_limit[i], min(self.follower_arms[name].joint_p_limit[i], value))

                # 移动平均滤波
                # filter_value = self.follower_arms[name].filters[i].update(clamped_value)

                # if abs(filter_value - self.filters[i].get_last()) / WINDOW_SIZE > 180 ##超180度/s位移限制，暂时不弄

                # 直接使用内存视图操作

                # self.follower_arms[name].joint_teleop_write[i] = filter_value
                self.follower_arms[name].joint_teleop_write[i] = clamped_value

            # 电机角度到夹爪开合度的换算
            giper_value = leader_pos[7] * GRIPPER_SCALE
            self.follower_arms[name].clipped_gripper = max(0, min(100, int(giper_value)))

            # 机械臂执行动作（调用透传API，控制gen72移动到目标位置）
            write_start = time.perf_counter()
            self.follower_arms[name].movej_canfd(self.follower_arms[name].joint_teleop_write)
            if self.frame_counter % 5 == 0:
                self.frame_counter = 0
                self.follower_arms[name].write_single_register(self.follower_arms[name].clipped_gripper)
            self.logs[f"write_follower_{name}_goal_pos_dt_s"] = time.perf_counter() - write_start

        print("end teleoperate")

        if not record_data:
            return

        follower_pos = {}
        for name in self.follower_arms:
            eight_byte_array = np.zeros(8, dtype=np.float32)
            
            now = time.perf_counter()
            joint_teleop_read = self.follower_arms[name].async_read_joint_degree()

            
            eight_byte_array[:7] = joint_teleop_read[:]

            eight_byte_array[7] = self.follower_arms[name].old_grasp
            eight_byte_array = np.round(eight_byte_array, 2)
            self.logs[f"read_follower_{name}_pos_dt_s"] = time.perf_counter() - now
            follower_pos[name] = torch.from_numpy(eight_byte_array)
        
            self.follower_arms[name].old_grasp=self.follower_arms[name].clipped_gripper

        #记录当前关节角度
        state = []
        for name in self.follower_arms:
            if name in follower_pos:
                state.append(follower_pos[name])
        state = torch.cat(state)

        #将关节目标位置添加到 action 列表中
        action = []
        for name in self.leader_arms:
            goal_eight_byte_array = np.zeros(8, dtype=np.float32)
            goal_eight_byte_array[:7] = self.follower_arms[name].joint_teleop_write[:]
            goal_eight_byte_array[7] = self.follower_arms[name].clipped_gripper
            follower_goal_pos = torch.from_numpy(goal_eight_byte_array)

            action.append(follower_goal_pos)
        action = torch.cat(action)

        # Capture images from cameras
        images = {}
        for name in self.cameras:
            now = time.perf_counter()
            images[name] = self.cameras[name].async_read()
            images[name] = torch.from_numpy(images[name])
            self.logs[f"read_camera_{name}_dt_s"] = self.cameras[name].logs["delta_timestamp_s"]
            self.logs[f"async_read_camera_{name}_dt_s"] = time.perf_counter() - now

        # Populate output dictionnaries and format to pytorch
        obs_dict, action_dict = {}, {}
        obs_dict["observation.state"] = state
        action_dict["action"] = action
        for name in self.cameras:
            obs_dict[f"observation.images.{name}"] = images[name]
        
        print("end teleoperate record")

        return obs_dict, action_dict



    # def capture_observation(self):
    #     if not self.is_connected:
    #         raise RobotDeviceNotConnectedError(
    #             "KochRobot is not connected. You need to run `robot.connect()`."
    #         )

    #     #调用从臂api获取当前关节角度 
    #     for name in self.leader_arms:
    #         now = time.perf_counter()
    #         self.pDll.Get_Joint_Degree(self.nSocket,self.joint_obs_read)  
    #         #夹爪通信获取当前夹爪开合度
    #         #   giper_read=ctypes.c_int()
    #         #   self.pDll.Get_Read_Holding_Registers(self.nSocket,1,40005,1,ctypes.byref(giper_read))
    #         #   #八位数组存储关节和夹爪数据
    #         self.joint_obs_present[:7]=self.joint_obs_read[:]
    #         #   self.joint_obs_present[7]=giper_read.value
    #         if self.gipflag_send==1:
    #             self.joint_obs_present[7]=100
    #         elif self.gipflag_send==0:
    #             self.joint_obs_present[7]=10
    #         # self.joint_obs_present = np.zeros(8)  # 创建一个包含八个0的 NumPy 数组
    #         self.logs[f"read_follower_{name}_pos_dt_s"] = time.perf_counter() - now

    #     # Create state by concatenating follower current position
    #     #上传当前机械臂状态
    #     state = []
    #     self.joint_obs_present = np.round(self.joint_obs_present, 2)
    #     joint_array_np = np.array( self.joint_obs_present)
    #     state = np.array([joint_array_np], dtype=np.float32)
    #     state = np.concatenate(state, dtype=np.float32)

    #     # Capture images from cameras
    #     images = {}
    #     for name in self.cameras:
    #         now = time.perf_counter()
    #         images[name] = self.cameras[name].async_read()
    #         self.logs[f"read_camera_{name}_dt_s"] = self.cameras[name].logs["delta_timestamp_s"]
    #         self.logs[f"async_read_camera_{name}_dt_s"] = time.perf_counter() - now

    #     # Populate output dictionnaries and format to pytorch
    #     obs_dict = {}
    #     obs_dict["observation.state"] = torch.from_numpy(state)
    #     for name in self.cameras:
    #         # Convert to pytorch format: channel first and float32 in [0,1]
    #         img = torch.from_numpy(images[name])
    #         img = img.type(torch.float32) / 255
    #         img = img.permute(2, 0, 1).contiguous()
    #         obs_dict[f"observation.images.{name}"] = img
    #     return obs_dict    def capture_observation(self):

    def capture_observation(self):
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                "KochRobot is not connected. You need to run `robot.connect()`."
            )

        follower_pos = {}
        for name in self.follower_arms:
            now = time.perf_counter()
            eight_byte_array = np.zeros(8, dtype=np.float32)
            joint_obs_read = self.follower_arms[name].async_read_joint_degree()

            #夹爪通信获取当前夹爪开合度
            # giper_read=ctypes.c_int()
            # self.pDll.Get_Read_Holding_Registers(self.nSocket,1,40000,1,ctypes.byref(giper_read))
            #   #八位数组存储关节和夹爪数据
            eight_byte_array[:7] = joint_obs_read[:]
            # self.joint_obs_present[7]=giper_read.value
            eight_byte_array[7] = self.follower_arms[name].old_grasp
            # self.joint_obs_present = np.zeros(8)  # 创建一个包含八个0的 NumPy 数组
            eight_byte_array = np.round(eight_byte_array, 2)
            follower_pos[name] = torch.from_numpy(eight_byte_array)
            self.logs[f"read_follower_{name}_pos_dt_s"] = time.perf_counter() - now

        # Create state by concatenating follower current position
        #上传当前机械臂状态
        state = []
        for name in self.follower_arms:
            if name in follower_pos:
                state.append(follower_pos[name])    
        state = torch.cat(state)

        # Capture images from cameras
        images = {}
        for name in self.cameras:
            now = time.perf_counter()
            images[name] = self.cameras[name].async_read()
            images[name] = torch.from_numpy(images[name])
            self.logs[f"read_camera_{name}_dt_s"] = self.cameras[name].logs["delta_timestamp_s"]
            self.logs[f"async_read_camera_{name}_dt_s"] = time.perf_counter() - now

        # Populate output dictionnaries and format to pytorch
        obs_dict = {}
        obs_dict["observation.state"] = state
        for name in self.cameras:
            obs_dict[f"observation.images.{name}"] = images[name]
        return obs_dict




    def send_action(self, action: torch.Tensor):
        """The provided action is expected to be a vector."""
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                "KochRobot is not connected. You need to run `robot.connect()`."
            )
        from_idx = 0
        to_idx = 8
        index = 0
        action_sent = []
        for name in self.follower_arms:

            goal_pos = action[index*8+from_idx:index*8+to_idx]
            index+=1

            for i in range(7):
                self.follower_arms[name].joint_send[i] = max(self.follower_arms[name].joint_n_limit[i], min(self.follower_arms[name].joint_p_limit[i], goal_pos[i]))
            
            
            self.follower_arms[name].movej_canfd(self.follower_arms[name].joint_send)
            # if (goal_pos[7]<50):
            #     # ret_giper = self.pDll.Write_Single_Register(self.nSocket, 1, 40000, int(follower_goal_pos_array[7]), 1, 1)
            #     ret_giper = self.pDll.Write_Single_Register(self.nSocket, 1, 40000, 0 , 1, 1)
            #     self.gipflag_send=0
            # #状态为闭合，且需要张开夹爪
            # if (goal_pos[7]>=50):
            #     # ret_giper = self.pDll.Write_Single_Register(self.nSocket, 1, 40000, int(follower_goal_pos_array[7]), 1, 1)
            #     ret_giper = self.pDll.Write_Single_Register(self.nSocket, 1, 40000, 100, 1, 1)
            #     self.gipflag_send=1
            gripper_value = max(0, min(100, int(goal_pos[7])))

            self.frame_counter += 1

            self.follower_arms[name].old_grasp = gripper_value
            if self.frame_counter % 5 == 0:
                self.frame_counter = 0
                self.follower_arms[name].write_single_register(gripper_value)
            action_sent.append(goal_pos)

        return torch.cat(action_sent)

    def disconnect(self):
        if not self.is_connected:
            raise RobotDeviceNotConnectedError(
                "KochRobot is not connected. You need to run `robot.connect()` before disconnecting."
            )
        
        for name in self.follower_arms:
            self.follower_arms[name].disconnect()

        for name in self.leader_arms:
            self.leader_arms[name].disconnect()

        for name in self.cameras:
            self.cameras[name].disconnect()

        self.is_connected = False

    def __del__(self):
        if getattr(self, "is_connected", False):
            self.disconnect()
