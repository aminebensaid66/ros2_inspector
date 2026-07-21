import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class TalkerNode(Node):
    def __init__(self):
        super().__init__("talker")
        self.pub = self.create_publisher(String, "/chatter", 10)
        self.timer = self.create_timer(1.0, self.timer_callback)

    def timer_callback(self):
        msg = String()
        msg.data = "hello"
        self.pub.publish(msg)


def main():
    rclpy.init()
    node = TalkerNode()
    rclpy.spin(node)
