# Judge拍摄资料映射

新评分默认附加GT媒体的文字拍摄时间和可读地点，图片由独立开关控制。
这些资料仅供直接对照参考答案的Judge使用；选择题选项映射和列表Jaccard不接收这些材料。

## 配置与生成

每个相册配置一个JSON文件，位于以下路径。

    <PHOTOBENCH_CONFIG_ROOT>/gt_media_metadata/<album_id>.json

从已审核媒体标注生成，并与活动素材逐份核对SHA：

    PYTHONPATH=apps/photobench/src .venv/bin/python scripts/build_gt_capture_context.py --album /absolute/path/to/reviewed-album --verify-against /absolute/path/to/active-album --output /absolute/path/to/config/gt_media_metadata/album-id.json

脚本读取相册manifest、图片标注和视频来源照片记录。输出已存在时拒绝覆盖。
独立配置文件可以更新，固定数据包与QA不原地修改。
地点取审核后的文字标注；视频来源照片采用已有地图记录中一致的保守行政范围。
不把最近地图要素认定为拍摄场所，也不在评分时调用在线逆地理接口。

## 数据结构

    {
      "schema_version": "gt-capture-context-v1",
      "album_id": "album-id",
      "annotation_version": "review-version",
      "sources": [{"path": "metadata/image_metadata.jsonl", "sha256": "source-hash"}],
      "entries": [{
        "media_type": "image",
        "media_id": "album3/example.jpg",
        "aliases": ["image:photos/example.jpg"],
        "relative_path": "photos/example.jpg",
        "media_sha256": "media-content-hash",
        "metadata": {
          "captured_at": "2018-01-01T12:00:00+08:00",
          "capture_time_note": "EXIF本地时间；时区根据GPS推定",
          "readable_location": "审核确认的地点范围",
          "location_note": "细地点边界不确定时采用上一级范围"
        }
      }]
    }

用media_type与media_id匹配，可声明带媒体类型前缀的别名；歧义会报错。
原GPS和来源证据可存于条目审计字段，提示词只取metadata中的四个文字字段，
以及视频source_captures中的逐张来源照片资料。
视频时间标明来源照片的拍摄时间范围，地点标明来源照片锚点范围。

## 冻结与恢复

新客户端提交gt_metadata_mode=readable_v1。服务端默认使用该模式；
显式传旧use_gt_metadata且无新模式的客户端仍采用旧协议。
新run与新补评分批次保存映射全文、文件SHA及标注版本，参与批次ID计算。
实际Judge输入和逐媒体材料保存在评分请求及材料记录中。
恢复已有批次和Agent断点续跑使用原快照。更新当前映射不会改变历史批次。

无映射文件、无匹配媒体或无字段时，材料明确记录“未记录”；
存在但无法读取、格式错误或身份冲突的映射会报错。
已启用图片且其读取或挂载失败，评分仍报错，不能静默只按文字评分。
地点资料不会传入Agent请求，也不构成Agent成功检索或交付的证据。
