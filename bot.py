import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter

# 初始化 NoneBot
nonebot.init()

# 注册适配器
driver = nonebot.get_driver()
driver.register_adapter(OneBotAdapter)

# 加载插件
nonebot.load_plugins("plugins")
nonebot.load_plugin("nonebot_plugin_boardgame")
nonebot.load_plugin("nonebot_plugin_hitokoto")
nonebot.load_plugin("nonebot_plugin_wife")
nonebot.load_plugin("nonebot_plugin_petpet")
nonebot.load_plugin("nonebot_plugin_memes")

if __name__ == "__main__":
    nonebot.run()
