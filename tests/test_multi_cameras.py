from lerobot.common.robot_devices.cameras.configs import (
    CameraConfig,
    IntelRealSenseCameraConfig,
    OpenCVCameraConfig,
)

from dataclasses import dataclass, field
from lerobot.common.robot_devices.cameras.utils import make_cameras_from_configs

import torch
import cv2

@dataclass
class Config():
    cameras: dict[str, CameraConfig] = field(
        default_factory=lambda: {
            "top": OpenCVCameraConfig(
                camera_index=6,
                fps=30,
                width=640,
                height=480,
            ),
            "left_wrist": OpenCVCameraConfig(
                camera_index=14,
                fps=30,
                width=640,
                height=480,
            ),
            "right_wrist": OpenCVCameraConfig(
                camera_index=22,
                fps=30,
                width=640,
                height=480,
            ),
        }
    )

    mock: bool = False

config = Config()

cameras = make_cameras_from_configs(config.cameras)
for name in cameras:
    cameras[name].connect()
while True:
    images, observation = {},{}
    for name in cameras:
        images[name] = cameras[name].async_read()
        images[name] = torch.from_numpy(images[name])

    for name in cameras:
        observation[f"observation.images.{name}"] = images[name]
    
    image_keys = [key for key in observation if "image" in key]
    for key in image_keys:
        cv2.imshow(key, cv2.cvtColor(observation[key].numpy(), cv2.COLOR_RGB2BGR))
    print("after show display_cameras ")
    keboard_key = cv2.waitKey(1)



# cameras = {}
# cameras['left'] = cv2.VideoCapture(6)
# print("left OK")
# cameras['top'] = cv2.VideoCapture(14)
# print("top OK")
# cameras['right'] = cv2.VideoCapture(22)
# print("right OK")
# images = {}
# while True:
#     for name in cameras:
#         ret, images[name] = cameras[name].read()
#         if not ret:
#             raise OSError(f"Can't capture color image from camera {name}. ret: {ret}")

#         cv2.imshow(name, images[name])
#         cv2.waitKey(1)