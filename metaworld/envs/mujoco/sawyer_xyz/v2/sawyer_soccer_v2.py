import threading
import open3d as o3d
import mujoco
import numpy as np
from gymnasium.spaces import Box
from scipy.spatial.transform import Rotation

from metaworld.envs import reward_utils
from metaworld.envs.asset_path_utils import full_v2_path_for
from metaworld.envs.mujoco.sawyer_xyz.sawyer_xyz_env import (
    SawyerXYZEnv,
    _assert_task_is_set,
)

from scene_point_cloud_processor import ScenePointCloudExtractor, PointCloudSaver


class SawyerSoccerEnvV2(SawyerXYZEnv):
    OBJ_RADIUS = 0.013
    TARGET_RADIUS = 0.07

    def __init__(self, tasks=None, render_mode=None, width=500, height=500):
        # goal_low = (-0.1, 0.8, 0.0)
        # goal_high = (0.1, 0.9, 0.0)
        hand_low = (-0.5, 0.40, 0.05)
        hand_high = (0.5, 1, 0.5)
        # obj_low = (-0.1, 0.6, 0.03)
        # obj_high = (0.1, 0.7, 0.03)

        # 设置渲染宽度和高度
        self.width = width
        self.height = height

        # 设置渲染模式和摄像机参数
        self.render_mode = render_mode
        self.camera_name = None
        self.camera_id = None

        self.visualizer = PointCloudSaver()  # 初始化保存器

        obj_low = (0, 0.65, 0.03)
        obj_high = (0, 0.65, 0.03)
        goal_low = (0, 0.85, 0.0)
        goal_high = (0, 0.85, 0.0)

        super().__init__(
            self.model_name,
            hand_low=hand_low,
            hand_high=hand_high,
            render_mode=render_mode,
        )

        if tasks is not None:
            self.tasks = tasks

        self.init_config = {
            "obj_init_pos": np.array([0, 0.6, 0.03]),
            "obj_init_angle": 0.3,
            # "hand_init_pos": np.array([0.0, 0.53, 0.05]),
            "hand_init_pos": np.array([0.0, 0.56, 0.05]),
        }
        self.goal = np.array([0.0, 0.9, 0.03])
        self.obj_init_pos = self.init_config["obj_init_pos"]
        self.obj_init_angle = self.init_config["obj_init_angle"]
        self.hand_init_pos = self.init_config["hand_init_pos"]

        self._random_reset_space = Box(
            np.hstack((obj_low, goal_low)),
            np.hstack((obj_high, goal_high)),
        )
        self.goal_space = Box(np.array(goal_low), np.array(goal_high))

    @property
    def model_name(self):
        return full_v2_path_for("sawyer_xyz/sawyer_soccer.xml")

    @_assert_task_is_set
    def evaluate_state(self, obs, action):
        obj = obs[4:7]
        (
            reward,
            tcp_to_obj,
            tcp_opened,
            target_to_obj,
            object_grasped,
            in_place,
        ) = self.compute_reward(action, obs)

        success = float(target_to_obj <= 0.07)
        near_object = float(tcp_to_obj <= 0.03)
        grasp_success = float(
            self.touching_object
            and (tcp_opened > 0)
            and (obj[2] - 0.02 > self.obj_init_pos[2])
        )
        info = {
            "success": success,
            "near_object": near_object,
            "grasp_success": grasp_success,
            "grasp_reward": object_grasped,
            "in_place_reward": in_place,
            "obj_to_target": target_to_obj,
            "unscaled_reward": reward,
        }

        return reward, info

    def _get_pos_objects(self):
        return self.get_body_com("soccer_ball")

    def _get_quat_objects(self):
        geom_xmat = self.data.body("soccer_ball").xmat.reshape(3, 3)
        return Rotation.from_matrix(geom_xmat).as_quat()

    # 标记：pointcloud
    def render(self, mode=''):
        """
        渲染环境。

        参数：
            mode (str): 渲染模式，可以是 'human'、'rgb_array'、'depth_array' 或 'pointcloud'。
            width (int, 可选): 渲染宽度。如果为 None，则使用环境的默认宽度。
            height (int, 可选): 渲染高度。如果为 None，则使用环境的默认高度。
            camera_name (str): 摄像机名称。

        返回：
            如果 mode 为 'pointcloud'，返回完整的场景点云数据；否则，返回渲染结果。
        """
        if mode == 'pointcloud':
            extractor = ScenePointCloudExtractor(
                self.model, 
                self.data, 
                task_related_body_names=["soccer_ball", "goal_whole", "hand"]
                )
            point_cloud = extractor.extract_point_cloud()

            # self.visualizer.save_point_cloud(point_cloud)  # 保存点云文件
            return point_cloud
        else:
            return super().render()


    def reset_model(self):
        self._reset_hand()
        self._target_pos = self.goal.copy()
        self.obj_init_angle = self.init_config["obj_init_angle"]

        goal_pos = self._get_state_rand_vec()
        self._target_pos = goal_pos[3:]
        while np.linalg.norm(goal_pos[:2] - self._target_pos[:2]) < 0.15:
            goal_pos = self._get_state_rand_vec()
            self._target_pos = goal_pos[3:]
        self.target_pos = self._target_pos
        self.obj_init_pos = np.concatenate((goal_pos[:2], [self.obj_init_pos[-1]]))
        self.model.body_pos[
            mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "goal_whole")
        ] = self._target_pos
        self._set_obj_xyz(self.obj_init_pos)
        self.maxPushDist = np.linalg.norm(
            self.obj_init_pos[:2] - np.array(self._target_pos)[:2]
        )

        return self._get_obs()

    def _gripper_caging_reward(self, action, obj_position, obj_radius):
        pad_success_margin = 0.05
        grip_success_margin = obj_radius + 0.01
        x_z_success_margin = 0.005

        tcp = self.tcp_center
        left_pad = self.get_body_com("leftpad")
        right_pad = self.get_body_com("rightpad")
        delta_object_y_left_pad = left_pad[1] - obj_position[1]
        delta_object_y_right_pad = obj_position[1] - right_pad[1]
        right_caging_margin = abs(
            abs(obj_position[1] - self.init_right_pad[1]) - pad_success_margin
        )
        left_caging_margin = abs(
            abs(obj_position[1] - self.init_left_pad[1]) - pad_success_margin
        )

        right_caging = reward_utils.tolerance(
            delta_object_y_right_pad,
            bounds=(obj_radius, pad_success_margin),
            margin=right_caging_margin,
            sigmoid="long_tail",
        )
        left_caging = reward_utils.tolerance(
            delta_object_y_left_pad,
            bounds=(obj_radius, pad_success_margin),
            margin=left_caging_margin,
            sigmoid="long_tail",
        )

        right_gripping = reward_utils.tolerance(
            delta_object_y_right_pad,
            bounds=(obj_radius, grip_success_margin),
            margin=right_caging_margin,
            sigmoid="long_tail",
        )
        left_gripping = reward_utils.tolerance(
            delta_object_y_left_pad,
            bounds=(obj_radius, grip_success_margin),
            margin=left_caging_margin,
            sigmoid="long_tail",
        )

        assert right_caging >= 0 and right_caging <= 1
        assert left_caging >= 0 and left_caging <= 1

        y_caging = reward_utils.hamacher_product(right_caging, left_caging)
        y_gripping = reward_utils.hamacher_product(right_gripping, left_gripping)

        assert y_caging >= 0 and y_caging <= 1

        tcp_xz = tcp + np.array([0.0, -tcp[1], 0.0])
        obj_position_x_z = np.copy(obj_position) + np.array(
            [0.0, -obj_position[1], 0.0]
        )
        tcp_obj_norm_x_z = np.linalg.norm(tcp_xz - obj_position_x_z, ord=2)
        init_obj_x_z = self.obj_init_pos + np.array([0.0, -self.obj_init_pos[1], 0.0])
        init_tcp_x_z = self.init_tcp + np.array([0.0, -self.init_tcp[1], 0.0])

        tcp_obj_x_z_margin = (
            np.linalg.norm(init_obj_x_z - init_tcp_x_z, ord=2) - x_z_success_margin
        )
        x_z_caging = reward_utils.tolerance(
            tcp_obj_norm_x_z,
            bounds=(0, x_z_success_margin),
            margin=tcp_obj_x_z_margin,
            sigmoid="long_tail",
        )

        assert right_caging >= 0 and right_caging <= 1
        gripper_closed = min(max(0, action[-1]), 1)
        assert gripper_closed >= 0 and gripper_closed <= 1
        caging = reward_utils.hamacher_product(y_caging, x_z_caging)
        assert caging >= 0 and caging <= 1

        if caging > 0.95:
            gripping = y_gripping
        else:
            gripping = 0.0
        assert gripping >= 0 and gripping <= 1

        caging_and_gripping = (caging + gripping) / 2
        assert caging_and_gripping >= 0 and caging_and_gripping <= 1

        return caging_and_gripping

    def compute_reward(self, action, obs):
        obj = obs[4:7]
        tcp_opened = obs[3]
        x_scaling = np.array([3.0, 1.0, 1.0])
        tcp_to_obj = np.linalg.norm(obj - self.tcp_center)
        target_to_obj = np.linalg.norm((obj - self._target_pos) * x_scaling)
        target_to_obj_init = np.linalg.norm((obj - self.obj_init_pos) * x_scaling)

        in_place = reward_utils.tolerance(
            target_to_obj,
            bounds=(0, self.TARGET_RADIUS),
            margin=target_to_obj_init,
            sigmoid="long_tail",
        )

        goal_line = self._target_pos[1] - 0.1
        if obj[1] > goal_line and abs(obj[0] - self._target_pos[0]) > 0.10:
            in_place = np.clip(
                in_place - 2 * ((obj[1] - goal_line) / (1 - goal_line)), 0.0, 1.0
            )

        object_grasped = self._gripper_caging_reward(action, obj, self.OBJ_RADIUS)

        reward = (3 * object_grasped) + (6.5 * in_place)

        if target_to_obj < self.TARGET_RADIUS:
            reward = 10.0
        return (
            reward,
            tcp_to_obj,
            tcp_opened,
            np.linalg.norm(obj - self._target_pos),
            object_grasped,
            in_place,
        )


class TrainSoccerv2(SawyerSoccerEnvV2):
    tasks = None

    def __init__(self):
        SawyerSoccerEnvV2.__init__(self, self.tasks)

    def reset(self, seed=None, options=None):
        return super().reset(seed=seed, options=options)


class TestSoccerv2(SawyerSoccerEnvV2):
    tasks = None

    def __init__(self):
        SawyerSoccerEnvV2.__init__(self, self.tasks)

    def reset(self, seed=None, options=None):
        return super().reset(seed=seed, options=options)
