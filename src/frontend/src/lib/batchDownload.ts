// 批量下载。浏览器(尤其手机 Safari)会拦截"一次触发多个下载",所以:
//  • 单个 → 直接下载(带视频标题文件名)
//  • 多个 → 后端打包成一个 zip,只触发一个下载 → 全平台都稳
import { api } from '../api/client'

function triggerDownload(url: string, filename: string): void {
  const a = document.createElement('a')
  a.href = url
  a.download = filename // 跨域时被忽略,真正文件名由服务端 Content-Disposition 决定
  a.rel = 'noopener'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}

// 下载选中的视频。返回下载的条数(0 表示没有可下的)。
export async function batchDownloadProjects(ids: string[]): Promise<number> {
  const list = ids.filter(Boolean)
  if (list.length === 0) return 0
  if (list.length === 1) {
    triggerDownload(api.projects.downloadUrl(list[0]), `${list[0]}.mp4`)
    api.projects.markDownloaded(list[0]).catch(() => {})
    return 1
  }
  // 多个 → 一个 zip(后端会顺带标记已下载)
  triggerDownload(api.projects.downloadZipUrl(list), 'media-buddy-videos.zip')
  return list.length
}
