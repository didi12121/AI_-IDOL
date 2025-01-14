import network
import socket
import time
import ujson
import urequests
from machine import UART, Pin
import struct

try:
    from machine import I2S  # 如果固件中原生支持 I2S
except:
    print("当前 MicroPython 固件可能不支持 I2S，需要使用自定义或第三方 I2S 库。")
    # 根据实际情况导入或初始化第三方 I2S 驱动
    pass

# ====== WiFi 参数 ======
SSID = "zhai"
PASSWORD = "15976905689"

# ====== 服务器地址 & 端口 ======
HOST = "192.168.110.188"  # PC服务器IP
HOST_PORT = 8888  # 服务器接收音频的端口
PLAY_BY_UUID_URL = "http://192.168.110.188:9000/playByUuid"  # Play By UUID 的URL

# ====== 本地UDP监听端口，用于接收服务器回传的数据 ======
LOCAL_PORT = 8889

# ====== I2S 引脚定义 (示例) ======
# 根据实际接线和固件 I2S API 做对应修改
I2S_RX_SCK = 33  # BCLK
I2S_RX_WS = 26  # LRCK
I2S_RX_SD = 25  # DIN (MIC输出)
I2S_RX_ID = 0  # I2S 端口号（示例）

I2S_TX_BCK = 22
I2S_TX_LRCK = 21
I2S_TX_DIN = 23
I2S_TX_ID = 1  # I2S 端口号（示例）

# ====== 音频配置 ======
SAMPLE_RATE = 16000
BITS_PER_SAMPLE = 16
CHANNELS = 1
BUFFER_LEN = 1024  # 单次采样大小

# ====== 全局变量 ======
isRecording = False
uart = None
udp_sock = None
# 在 main() 或相应作用域中：
silence_count = 0            # 当前已经连续检测到多少帧是静音
SILENCE_FRAMES_THRESHOLD = 15 # 连续多少帧静音就执行 end

isPlaying = False
# -----------------------------------------------------------------------------
# 1. 初始化 WiFi
# -----------------------------------------------------------------------------
def setup_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("正在连接 WiFi: {} ...".format(SSID))
        wlan.connect(SSID, PASSWORD)
        retry = 0
        while not wlan.isconnected():
            time.sleep(0.5)
            retry += 1
            if retry > 60:
                print("连接超时，重启设备...")
                import machine
                machine.reset()
    print("WiFi 已连接，IP地址:", wlan.ifconfig()[0])


# -----------------------------------------------------------------------------
# 2. 初始化 UART
# -----------------------------------------------------------------------------
def setup_uart():
    global uart
    # UART2 (TX=17, RX=16), 9600 波特率
    uart = UART(2, baudrate=9600, tx=17, rx=16)
    print("UART 已初始化。")


# -----------------------------------------------------------------------------
# 3. 初始化 UDP Socket 并监听服务器回传
# -----------------------------------------------------------------------------
def setup_udp():
    global udp_sock
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.bind(('0.0.0.0', LOCAL_PORT))  # 监听本地端口
    udp_sock.settimeout(0)  # 非阻塞模式
    print("UDP Socket 已在端口 {} 监听。".format(LOCAL_PORT))


# -----------------------------------------------------------------------------
# 4. 初始化 I2S (采集 & 播放)
# -----------------------------------------------------------------------------
def setup_i2s():
    # ========== 录音 I2S (RX) ==========
    try:
        i2s_in = I2S(
            I2S_RX_ID,
            sck=Pin(I2S_RX_SCK),
            ws=Pin(I2S_RX_WS),
            sd=Pin(I2S_RX_SD),
            mode=I2S.RX,
            bits=BITS_PER_SAMPLE,
            format=I2S.MONO if CHANNELS == 1 else I2S.STEREO,
            rate=SAMPLE_RATE,
            ibuf=BUFFER_LEN * 2  # 实际大小根据固件特性可调整
        )
        print("I2S RX 已启动.")
    except Exception as e:
        print("I2S RX 初始化失败:", e)
        i2s_in = None

    # ========== 播放 I2S (TX) ==========
    try:
        i2s_out = I2S(
            I2S_TX_ID,
            sck=Pin(I2S_TX_BCK),
            ws=Pin(I2S_TX_LRCK),
            sd=Pin(I2S_TX_DIN),
            mode=I2S.TX,
            bits=BITS_PER_SAMPLE,
            format=I2S.STEREO if CHANNELS == 2 else I2S.MONO,
            rate=SAMPLE_RATE * 2,
            ibuf=BUFFER_LEN * 2
        )
        print("I2S TX 已启动.")
    except Exception as e:
        print("I2S TX 初始化失败:", e)
        i2s_out = None

    return i2s_in, i2s_out


# -----------------------------------------------------------------------------
# 5. 发送开始/结束标志
# -----------------------------------------------------------------------------
def send_start_signal():
    msg = b"START"
    udp_sock.sendto(msg, (HOST, HOST_PORT))
    print("已发送开始标识 START。")


def send_end_signal():
    msg = b"END"
    udp_sock.sendto(msg, (HOST, HOST_PORT))
    print("已发送结束标识 END。")


# -----------------------------------------------------------------------------
# 6. 根据 UUID 从服务器获取音频并播放
# -----------------------------------------------------------------------------
def playAudioFromUuidFast(i2s_out, uuid):
    if not uuid:
        print("无效的 UUID。")
        return

    # 拼接请求URL
    url = "{}?id={}".format(PLAY_BY_UUID_URL, uuid)
    print("开始请求音频文件: ", url)

    try:
        r = urequests.get(url)
    except Exception as e:
        print("HTTP 请求失败:", e)
        return

    if r.status_code == 200:
        content_type = r.headers.get("Content-Type", "")
        print("Content-Type:", content_type)

        # 这里简单起见，不做 content_type 校验
        print("开始接收并播放音频数据...")

        # 流式读取
        chunk_size = 1024
        while True:
            chunk = r.raw.read(chunk_size)
            if not chunk or len(chunk) == 0:
                break
            # 将数据写入 I2S 播放
            if i2s_out:
                try:
                    i2s_out.write(chunk)
                except Exception as e:
                    print("I2S 播放出错:", e)
                    break
        print("播放结束。")
    else:
        print("HTTP 响应码非 200, code =", r.status_code)

    r.close()


# ---------------------------------------------------------------------------
# 当前是否静音检测方法
# ---------------------------------------------------------------------------
def is_silence_frame(s_buffer, threshold=2000):
    """
        根据缓冲区计算音频峰值，与阈值比较来判定是否“静音”。
        threshold: 需要根据实际环境噪声和麦克风灵敏度来调整
    """

    # s_buffer 是 bytes/bytearray，长度假设 = N * 2 (N个16位采样)
    sample_count = len(s_buffer) // 2
    # 将二进制缓冲区解析为 Python int 列表
    # '<' 表示 little-endian, 'h' 表示 16位有符号整数
    samples = struct.unpack('<' + 'h' * sample_count, s_buffer)

    # 计算一帧的峰值
    peak_value = max(abs(s) for s in samples)
    print(peak_value)
    # 与阈值比较
    return peak_value < threshold


# -----------------------------------------------------------------------------
# 7. 主函数
# -----------------------------------------------------------------------------
def main():
    global isRecording, isPlaying

    # 1) WiFi / UART / UDP / I2S 初始化
    setup_wifi()
    setup_uart()
    setup_udp()
    i2s_in, i2s_out = setup_i2s()

    r_buffer = bytearray(BUFFER_LEN * 2)  # 用于UDP接收
    s_buffer = bytearray(BUFFER_LEN * 2)  # 用于采集发送

    while True:
        # ========== (A) 接收 UDP 数据 ==========
        try:
            data, addr = udp_sock.recvfrom(1024)  # 非阻塞模式，若无数据会抛 OSError
            text = data.decode('utf-8')
            print("UDP 收到数据:", text)
            # 使用 ujson 解析
            obj = ujson.loads(text)
            # 如果有 "id" 字段，则播放对应音频
            if "id" in obj:
                uuid = obj["id"]
                print("[JSON] id =", uuid)
                playAudioFromUuidFast(i2s_out,uuid)
        except OSError:
            pass

        # ========== (B) 读取串口命令 ==========
        if uart.any():
            line = uart.readline().decode('utf-8').strip()
            if line:
                if line in 'start':
                    print("串口收到命令:", line)
                    if line.lower() != "":
                        if not isRecording:
                            send_start_signal()
                            isRecording = True

                            print("开始传输音频数据。")
                            time.sleep_ms(500)

                        else:
                            print("已经在录音状态，无需重复 start。")
                    elif line.lower() == "end":
                        if isRecording:
                            send_end_signal()
                            isRecording = False
                            print("停止传输音频数据。")
                        else:
                            print("未在录音状态，无需 end。")
                    else:
                        # 也可以将其它串口输入视为 UUID，调用播放函数
                        pass

        # ========== (C) 若处于录音状态，就持续采集并通过UDP发送 ==========
        if isRecording and i2s_in:
            try:
                # 从 I2S 读取一帧音频数据
                nbytes = i2s_in.readinto(s_buffer)
                if nbytes:
                    # 1) 先检查这帧是否静音
                    if is_silence_frame(s_buffer, threshold=500):
                        silence_count += 1
                    else:
                        silence_count = 0  # 如果检测到有声音，就重置静音计数

                    # 2) 若连续静音帧数达标，则执行 end 逻辑
                    if silence_count >= SILENCE_FRAMES_THRESHOLD:
                        print("检测到连续无声，执行 end 逻辑...")
                        send_end_signal()  # 你的 end 标识发送函数
                        isRecording = False
                        silence_count = 0  # 清零
                    else:
                        # 通过 UDP 发送到服务器
                        udp_sock.sendto(s_buffer, (HOST, HOST_PORT))
            except Exception as e:
                print("I2S 录音或 UDP 发送错误:", e)
        time.sleep_ms(10)  # 稍微延时，防止循环过快


# -----------------------------------------------------------------------------
# 启动
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()
