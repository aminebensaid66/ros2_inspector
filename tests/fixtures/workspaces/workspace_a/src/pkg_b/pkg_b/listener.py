import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class ListenerNode(Node):
    def __init__(self):
        super().__init__("listener")
        self.sub = self.create_subscription(String, "/chatter", self.callback, 10)
        self.cli = self.create_client(String, "/ping")

    def callback(self, msg):
        pass


def main():
    rclpy.init()
    node = ListenerNode()
    rclpy.spin(node)
