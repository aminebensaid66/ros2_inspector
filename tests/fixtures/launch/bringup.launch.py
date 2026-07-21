from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription(
        [
            Node(
                package="pkg_a",
                executable="talker",
                name="talker_node",
                remappings=[("/chatter", "/my_chatter")],
            ),
            Node(
                package="pkg_b",
                executable="listener",
                name="listener_node",
                namespace="/robot",
            ),
        ]
    )
