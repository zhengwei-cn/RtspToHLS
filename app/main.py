import os
import subprocess
import signal
import threading
import toml
from flask import Flask, request, jsonify, send_from_directory, render_template
from onvif import ONVIFCamera

app = Flask(__name__)

# 记录流状态 {stream_id: {process, output_dir, url}}
streams = {}
base_dir = os.path.dirname(os.path.abspath(__file__))


def get_rtsp_url(host, port, username, password, profile_name="quality_h264"):
    """
    通过 ONVIF 获取 RTSP 地址
    :param host: 设备 IP 地址
    :param port: 设备端口（通常为 80 或 554）
    :param username: ONVIF 认证用户名
    :param password: ONVIF 认证密码
    :param profile_name: 媒体配置文件名
    :return: RTSP 流地址
    """
    try:
        # 创建摄像头实例
        cam = ONVIFCamera(host, port, username, password)

        # 获取媒体服务
        media_service = cam.create_media_service()

        # 获取所有可用的媒体配置文件
        profiles = media_service.GetProfiles()

        # 查找匹配的配置文件
        profile = next((p for p in profiles if profile_name in p.Name), profiles[0])

        # 获取流 URI
        stream_uri = media_service.GetStreamUri(
            {
                "StreamSetup": {
                    "Stream": "RTP-Unicast",  # 或者 RTP-Multicast
                    "Transport": {"Protocol": "RTSP"},
                },
                "ProfileToken": profile.token,
            }
        )
        return stream_uri.Uri

    except Exception as e:
        print(f"Error retrieving RTSP URL: {e}")
        return None


def start_ffmpeg(rtsp_url, stream_id):
    output_dir = os.path.join(base_dir, "output", stream_id)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    output_file = os.path.join(output_dir, "output.m3u8")
    ffmpeg_cmd = [
        "ffmpeg",
        "-rtsp_transport",
        "tcp",  # 使用 TCP 确保稳定性
        "-fflags",
        "nobuffer",  # 禁用输入缓冲
        "-flags",
        "low_delay",  # 启用低延时模式
        "-strict",
        "experimental",  # 启用实验性功能
        "-i",
        rtsp_url,
        "-c:v",
        "libx264",  # 使用高效的 H.264 编码器
        "-preset",
        "ultrafast",  # 编码器速度优先
        "-tune",
        "zerolatency",  # 实时流优化
        "-x264opts",
        "keyint=10:min-keyint=10:scenecut=-1",  # 更短的关键帧间隔
        "-g",
        "24",  # GOP 设置为更小的 10 帧
        "-max_delay",
        "0",  # 禁用输入流的延迟缓冲
        "-hls_time",
        "0.5",  # 将 HLS 分片的持续时间减少到 0.5 秒
        "-hls_list_size",
        "2",  # 播放列表仅保留最近的 2 个分片
        "-hls_flags",
        "delete_segments+append_list",  # 删除旧分片并避免重写 m3u8
        "-f",
        "hls",  # 输出 HLS 格式
        output_file,
    ]
    # ffmpeg_cmd = [
    #     "ffmpeg",
    #     "-rtsp_transport",
    #     "tcp",  # 使用 TCP 确保稳定性，减少丢包
    #     "-fflags",
    #     "nobuffer",  # 禁用输入缓冲
    #     "-flags",
    #     "low_delay",  # 启用低延时标志
    #     "-i",
    #     rtsp_url,
    #     "-c:v",
    #     "libx264",  # 使用 x264 编码器
    #     "-preset",
    #     "ultrafast",  # 优化为最快的编码速度
    #     "-tune",
    #     "zerolatency",  # 针对实时流优化编码
    #     "-x264opts",
    #     "keyint=15:min-keyint=15:scenecut=-1",  # 减少关键帧间隔，恒定间隔
    #     "-g",
    #     "15",  # 设置 GOP（关键帧组大小）
    #     "-hls_time",
    #     "1",  # 每个 HLS 分片的持续时间（1 秒）
    #     "-hls_list_size",
    #     "3",  # 播放列表中保留 3 个分片
    #     "-hls_flags",
    #     "delete_segments",  # 实时删除旧分片
    #     "-f",
    #     "hls",  # 输出 HLS 格式
    #     output_file,
    # ]

    try:
        process = subprocess.Popen(
            ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        streams[stream_id] = {
            "process": process,
            "output_dir": output_dir,
            "url": f"/output/{stream_id}/output.m3u8",
        }
        stdout, stderr = process.communicate()
        print("FFmpeg stdout:", stdout.decode())
        print("FFmpeg stderr:", stderr.decode())
    except Exception as e:
        print(f"Error starting FFmpeg: {e}")


# 停止 FFmpeg 转码
def stop_ffmpeg(stream_id):
    if stream_id in streams:
        process = streams[stream_id]["process"]
        # 停止进程
        try:
            process.terminate()
        except PermissionError:
            print("PermissionError: [WinError 5] Access is denied")
        process.wait()

        try:
            # 删除输出文件夹
            output_dir = streams[stream_id]["output_dir"]
            if os.path.exists(output_dir):
                for root, dirs, files in os.walk(output_dir, topdown=False):
                    for name in files:
                        os.remove(os.path.join(root, name))
                    for name in dirs:
                        os.rmdir(os.path.join(root, name))
                os.rmdir(output_dir)
        except Exception as e:
            print(f"Error deleting output directory: {e}")

        del streams[stream_id]


# 启动转码接口
@app.route("/start", methods=["GET"])
def start_stream():
    brand = request.args.get("brand")
    if not brand:
        return jsonify({"error": "Missing brand"}), 400
    ip = request.args.get("ip")
    if not ip:
        return jsonify({"error": "Missing ip"}), 400

    account = request.args.get("account")
    if not account:
        return jsonify({"error": "Missing account"}), 400

    password = request.args.get("password")
    password = password.replace("@", "\u0040")
    if not password:
        return jsonify({"error": "Missing password"}), 400

    stream_id = request.args.get("stream_id")
    if not stream_id:
        return jsonify({"error": "Missing stream_id"}), 400

    ch = request.args.get("ch", "1")
    subtype = request.args.get("subtype", "1")

    cfg = app.config["app"]["video"][brand]
    if cfg is None:
        cfg = app.config["app"]["video"]["hikvision"]

    cfg_subtype = app.config["app"][brand]
    if cfg_subtype is not None:
        if subtype == "0":
            subtype = cfg_subtype["main"]
        else:
            subtype = cfg_subtype["sub"]

    rtsp_url = ""
    if brand == "rtsp":
        rtsp_url = (
            cfg.replace("{account}", account)
            .replace("{password}", password)
            .replace("{url}", ip)
        )
    elif brand == "onvif":
        try:
            rtsp_url = get_rtsp_url(ip, 554, account, password)
        except Exception as e:
            print(f"Error getting rtsp_url: {e}")
            return jsonify({"error": "Failed to get rtsp_url"}), 400
    else:
        rtsp_url = (
            cfg.replace("{ip}", ip)
            .replace("{account}", account)
            .replace("{password}", password)
            .replace("{ch}", ch)
            .replace("{subtype}", subtype)
        )

    print(f"{rtsp_url =}")
    if not rtsp_url or not stream_id:
        return jsonify({"error": "Missing rtsp_url or stream_id"}), 400

    if stream_id in streams:
        return jsonify({"error": f"Stream with ID {stream_id} already running"}), 400

    try:
        # start_ffmpeg(rtsp_url, stream_id)
        # 为了避免start_ffmpeg长时间阻塞，改为使用线程
        t = threading.Thread(target=start_ffmpeg, args=(rtsp_url, stream_id))
        t.start()
    except Exception as e:
        print(f"Error starting FFmpeg: {e}")
        return jsonify({"error": "Failed to start FFmpeg"}), 400
    return jsonify(
        {"message": "Stream started", "hls_url": f"/output/{stream_id}/output.m3u8"}
    )


# 停止转码接口
@app.route("/stop", methods=["GET"])
def stop_stream():
    stream_id = request.args.get("stream_id")
    if not stream_id:
        return jsonify({"error": "Missing stream_id"}), 400

    if stream_id not in streams:
        return jsonify({"error": f"No stream found with ID {stream_id}"}), 404

    stop_ffmpeg(stream_id)
    return jsonify({"message": "Stream stopped and files deleted"})


# 查询所有流状态接口
@app.route("/status", methods=["GET"])
def list_streams():
    return jsonify(
        {
            "active_streams": {
                stream_id: stream["url"] for stream_id, stream in streams.items()
            }
        }
    )


# 提供 HLS 文件的路由
@app.route("/output/<stream_id>/<path:filename>")
def serve_hls(stream_id, filename):
    return send_from_directory(os.path.join("output", stream_id), filename)


# 首页测试页面
@app.route("/")
def index():
    return render_template("index.html", streams=streams)


if __name__ == "__main__":
    config_path = os.path.join(base_dir, "config.toml")
    with open(config_path, "r") as f:
        config = toml.load(f)

    app.config["app"] = config["app"]
    app.run(host="0.0.0.0", port=5000, debug=True)
