
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

def setup_logger(name='bot', log_dir='logs', log_file='bot.log', level=logging.INFO):
    """
    配置日志系统
    :param name: 日志记录器名称
    :param log_dir: 日志目录
    :param log_file: 日志文件名
    :param level: 日志级别
    :return: logger 实例
    """
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # 避免重复添加 Handler
    if logger.handlers:
        return logger

    # 格式化器
    # 格式: [时间] [级别] [模块] 消息
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 1. 文件处理器 (RotatingFileHandler)
    # 单个日志文件最大 10MB，保留最近 5 个文件
    file_path = os.path.join(log_dir, log_file)
    file_handler = RotatingFileHandler(
        file_path,
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    # 2. 控制台处理器 (StreamHandler)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    return logger

# 创建全局 logger 实例
# 其他模块可以直接 import logger 使用
logger = setup_logger()
