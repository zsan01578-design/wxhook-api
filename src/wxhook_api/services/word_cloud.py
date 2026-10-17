"""Local word frequencies; chat text never leaves the server."""

from collections import Counter
import re

import jieba


STOP_WORDS = set("""
的 了 是 我 你 他 她 它 们 在 和 与 就 都 而 及 或 这 那 也 又 还 很 有 没 不 吗 呢 啊 哦 嗯 吧 呀 啦 哈 哈哈 哈哈哈
我们 你们 他们 她们 它们 自己 什么 怎么 为什么 这个 那个 这些 那些 一个 一些 这样 那样 这里 那里
可以 可能 但是 因为 所以 如果 然后 还是 就是 不是 没有 不要 不用 已经 现在 时候 知道 觉得 真的 这么 那么
一下 一点 一起 一直 比较 还有 也是 都是 只是 还是 是否 以及 进行 通过 关于 来自 大家 好的 好吧 谢谢
the a an and or of to in is are was were be been it this that for with on at as by from i you he she we they my your our not yes no
""".split())
TOKEN = re.compile(r"[\u3400-\u9fff]+|[A-Za-z][A-Za-z0-9+#.-]*")
LINK_OR_ID = re.compile(r"https?://\S+|www\.\S+|\bwxid_[\w-]+\b|\b[\w-]+@chatroom\b", re.I)
MENTION = re.compile(r"@[^\s@\u2005]+")


def frequent_words(messages: list[str], limit: int = 40) -> list[dict]:
    counts = Counter()
    for text in messages:
        text = re.sub(r"^[A-Za-z0-9_.-]{1,256}:\r?\n", "", text)
        # Text messages sometimes contain serialized XML; do not count its fields.
        if text.lstrip().startswith(("<", "&lt;")):
            continue
        text = MENTION.sub(" ", LINK_OR_ID.sub(" ", text))
        text = re.sub(r"\[[^\]\n]{1,30}\]", " ", text)
        for chunk in TOKEN.findall(text):
            words = jieba.cut(chunk, HMM=False) if re.search(r"[\u3400-\u9fff]", chunk) else [chunk]
            for word in words:
                word = word.strip(".-").lower()
                if 2 <= len(word) <= 20 and word not in STOP_WORDS and not word.isdigit():
                    counts[word] += 1
    return [{"text": word, "count": count} for word, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]
