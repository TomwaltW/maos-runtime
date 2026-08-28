"""业务域层 —— 每个域一个子包，只放业务对象与该域的守卫。

内核（contracts/ runtime/ core/）对本目录零依赖：换域只换这里，
`maos/contracts/**` 与 `maos/runtime/**` 一行不改（铁律 9）。
"""
