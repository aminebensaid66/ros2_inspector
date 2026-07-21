#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/string.hpp"
#include "geometry_msgs/msg/twist.hpp"

class MinimalPublisher : public rclcpp::Node
{
public:
  MinimalPublisher() : Node("minimal_publisher")
  {
    publisher_ = this->create_publisher<std_msgs::msg::String>("/chatter", 10);
    subscriber_ = this->create_subscription<geometry_msgs::msg::Twist>(
      "/cmd_vel", 10,
      std::bind(&MinimalPublisher::cmd_callback, this, std::placeholders::_1));
    service_ = this->create_service<std_srvs::srv::SetBool>(
      "/set_mode",
      std::bind(&MinimalPublisher::set_mode_cb, this,
                std::placeholders::_1, std::placeholders::_2));
  }

private:
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr publisher_;
  rclcpp::Subscription<geometry_msgs::msg::Twist>::SharedPtr subscriber_;
  rclcpp::Service<std_srvs::srv::SetBool>::SharedPtr service_;

  void cmd_callback(const geometry_msgs::msg::Twist::SharedPtr) {}
  void set_mode_cb(const std::shared_ptr<std_srvs::srv::SetBool::Request>,
                   std::shared_ptr<std_srvs::srv::SetBool::Response>) {}
};

int main(int argc, char * argv[])
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<MinimalPublisher>());
  rclcpp::shutdown();
  return 0;
}
