"""Build the Chinese handbook with ReportLab and embeddable CJK TrueType fonts.

python manual_zh.py --regular /path/Regular.ttf --bold /path/Bold.ttf
"""
import argparse
import os
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph


W, H = A4
M = 47
WIDTH = W - 2 * M
INK, TEAL, GRAY, HAIR, PALE = [colors.HexColor(x) for x in
    ("#182e33", "#11615e", "#4a6065", "#bacdc9", "#eff5f3")]
CHAPTERS = ["安装与入门", "一场两题的练习", "建立比赛草稿", "导入测试数据",
            "导出与验收", "三种赛制", "选手的工作目录", "故障排查与命令"]


def register_fonts(regular, bold):
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    regular = regular or next((p for p in [windows / "Deng.ttf",
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")] if p.is_file()), None)
    bold = bold or (windows / "Dengb.ttf" if (windows / "Dengb.ttf").is_file() else regular)
    if not regular or not bold:
        raise SystemExit("请用 --regular 和 --bold 指定可嵌入的中文 TrueType 字体。")
    pdfmetrics.registerFont(TTFont("BodyCN", str(regular)))
    pdfmetrics.registerFont(TTFont("BoldCN", str(bold)))


class Guide:
    def __init__(self, out):
        self.c = Canvas(str(out), pagesize=A4, pageCompression=1)
        self.c.setTitle("SelfEval by Kamome · 组赛与参赛手册")
        self.c.setAuthor("SelfEval by Kamome")
        self.c.setSubject("Linux 本地编程比赛的制作、分发和评测")
        self.number = 0
        self.used = []

    def label(self, x, y, text, size=10, font="BodyCN", color=INK):
        self.c.setFont(font, size)
        self.c.setFillColor(color)
        self.c.drawString(x, y, text)

    def line(self, x, y, end):
        self.c.setStrokeColor(HAIR)
        self.c.setLineWidth(0.6)
        self.c.line(x, y, end, y)

    def wrap(self, text, width, size, leading, font="BodyCN", color=INK):
        style = ParagraphStyle("p", fontName=font, fontSize=size, leading=leading,
                               textColor=color, alignment=TA_LEFT, wordWrap="CJK",
                               splitLongWords=0, spaceBefore=0, spaceAfter=0)
        para = Paragraph(escape(text), style)
        _, height = para.wrap(width, H)
        return para, height

    def page(self, title, summary):
        if self.number:
            self.finish_page()
        self.number += 1
        key = "ch" + str(self.number)
        self.c.bookmarkPage(key)
        self.c.addOutlineEntry(CHAPTERS[self.number - 1], key)
        self.label(M, H - 37, "SelfEval", 13, "Helvetica-Bold", TEAL)
        self.label(M + 54, H - 37, "by Kamome", 10, "Helvetica", GRAY)
        self.label(W - M - 112, H - 37, "使用手册  /  Linux  1.2", 9.5, color=GRAY)
        self.line(M, H - 48, W - M)
        self.label(M, H - 80, f"{self.number:02}     " + CHAPTERS[self.number - 1], 10, "BoldCN", TEAL)
        self.label(M, H - 122, title, 28, "BoldCN")
        self.y = H - 142
        self.p(summary, size=12, leading=20, color=GRAY)
        self.y -= 8

    def p(self, text, size=11.5, leading=19.2, color=INK):
        para, height = self.wrap(text, WIDTH, size, leading, color=color)
        para.drawOn(self.c, M, self.y - height)
        self.y -= height + 8

    def h(self, title):
        self.y -= 5
        self.label(M, self.y - 15, title, 14.2, "BoldCN", TEAL)
        self.y -= 27

    def code(self, lines, caption=None):
        if isinstance(lines, str):
            lines = lines.splitlines()
        top = self.y
        height = 19 + 14 * len(lines) + (19 if caption else 0)
        self.c.setFillColor(PALE)
        self.c.roundRect(M, top - height, WIDTH, height, 5, fill=1, stroke=0)
        yy = top - 16
        if caption:
            self.label(M + 12, yy, caption, 9.4, "BoldCN", TEAL)
            yy -= 19
        for line in lines:
            if pdfmetrics.stringWidth(line, "Courier", 9.8) > WIDTH - 24:
                raise ValueError(f"code line too wide: {line}")
            self.label(M + 12, yy, line, 9.8, "Courier")
            yy -= 14
        self.y = top - height - 10

    def rows(self, items, key_width=99):
        for key, value in items:
            left, a = self.wrap(key, key_width-10, 11.2, 18, "BoldCN", TEAL)
            right, b = self.wrap(value, WIDTH-key_width-4, 11.4, 19)
            high = max(a,b)
            self.line(M, self.y, W-M)
            left.drawOn(self.c, M, self.y - 7 - a)
            right.drawOn(self.c, M + key_width, self.y - 7 - b)
            self.y -= high + 15
        self.line(M, self.y, W-M)
        self.y -= 10

    def note(self, name, text):
        para, h = self.wrap(text, WIDTH-28, 11, 18)
        b = h+38
        self.c.setFillColor(PALE)
        self.c.rect(M,self.y-b,WIDTH,b,fill=1,stroke=0)
        self.c.setFillColor(TEAL)
        self.c.rect(M,self.y-b,2,b,fill=1,stroke=0)
        self.label(M+13,self.y-16,name,10.2,"BoldCN",TEAL)
        para.drawOn(self.c,M+13,self.y-23-h)
        self.y-=b+11

    def finish_page(self):
        self.used.append(round(self.y,1))
        if self.y < 46:
            raise ValueError(f"page {self.number} overflows, end at {self.y:.1f}")
        self.line(M, 36, W-M)
        self.label(M, 22, "SelfEval Studio  /  SelfEval 选手端", 8.5, color=GRAY)
        self.label(W-M-32, 22, f"{self.number:02} / 08", 9, "Helvetica", GRAY)
        self.c.showPage()

    def save(self):
        self.finish_page()
        self.c.save()
        print("Page end positions (pt):", self.used)


def handbook(out):
    g = Guide(out)
    g.page("组赛与参赛手册", "在一台 Linux 电脑上制作比赛，再把一个 .seexam 文件交给选手。本手册用 Hello World 和 A+B 两道题走完这个过程。")
    g.h("安装后，从应用菜单选择入口")
    g.rows([("SelfEval Studio", "组题人使用。准备题面、数据和计分规则，保存可编辑的草稿，导出给选手的比赛包。"),
            ("SelfEval", "选手使用。打开比赛包，选择代码目录，按比赛规定查看题面、提交程序和查看结果。")], 110)
    g.code("sudo apt install ./SelfEval-Linux.deb", "Debian / Ubuntu · 在安装包所在目录执行")
    g.p("更新时安装新版同名文件，关闭旧窗口后重新打开。使用便携版时，解压后进入 selfeval-linux 目录，执行 sh install.sh 即可为当前 Linux 用户添加这两个应用菜单入口。")
    g.h("按手头的事情查阅")
    for num, name, desc in [(2,"跟着做一次","用示例工程导出第一场比赛"), (3,"自己组赛","建草稿、选赛制、安排题序"),
                           (4,"准备数据","从本地测试对或现有 QOJ ZIP 导入"), (5,"发给选手","用正确程序和错误程序试跑"),
                           (6,"选择规则","比较三种赛制及开始、截止行为"), (7,"参加比赛","放置代码、提交多题、重开软件"),
                           (8,"解决问题","排查导出与评测错误，查常用命令")]:
        top=g.y
        g.label(M, top-13, f"{num:02}", 11, "Helvetica-Bold", TEAL)
        g.label(M+29, top-13, name, 10.8, "BoldCN")
        g.label(M+119, top-13, desc, 10.5, color=GRAY)
        g.c.linkRect("", "ch"+str(num), (M,top-18,W-M,top+1), relative=0, thickness=0)
        g.y-=23

    g.page("从 Hello & Sum 开始", "先用已经填好的工程试一次，检查本机能否从组赛一直走到提交。示例采用 ICPC 赛制，练习时长为 30 分钟。")
    g.h("取得可编辑的示例")
    g.p("解压 SelfEval-Studio-example.zip，得到 hello-sum 目录。如果手边是源码，也可以在源码根目录运行下面的命令生成同样的工程。生成器接受空目录或新目录。")
    g.code("python3 examples/make_demo.py hello-sum")
    g.code(["hello-sum/", "  contest.json              ", "  statements/booklet.pdf",
            "  statements/A.pdf          statements/B.pdf", "  packages/A.zip            packages/B.zip",
            "  data/A/tests/             data/A/samples/", "  data/B/tests/             data/B/samples/"],"工程中实际使用的文件")
    g.rows([("1   打开", "启动 SelfEval Studio，点「打开草稿…」，选择 hello-sum/contest.json。题目表中应有 A Hello World 和 B A+B。"),
            ("2   导出", "点「导出比赛包…」，把文件存为 hello-sum.seexam。等窗口提示生成成功，再把这个文件交给选手端。"),
            ("3   开始", "启动 SelfEval，点「打开考试…」选择刚导出的文件。选择一个单独的空目录作为选手目录，再点「开始比赛」。"),
            ("4   试交", "在选手目录保存 a.cpp 和 b.cpp。点「提交评测…」，勾选 A、B 后提交。两份参考代码及预期结果见第 5 页。")], 71)
    g.p("contest.json 记录组赛设置并引用旁边的材料。需要把工程交给另一位组题人时，复制整个 hello-sum 目录。")

    g.page("从空白建立比赛草稿", "先确定赛制、比赛时间和题目顺序。草稿保存的是编辑进度，导出的 .seexam 才是选手在考试机上打开的文件。")
    g.h("整场设置")
    g.rows([("比赛名、整册 PDF", "输入选手会看到的比赛名，选择一份按比赛题序合并好的题册。逐题查看时使用各题独立的 PDF，两种文件都需要准备。"),
            ("赛制", "ICPC 适合赛中按题返回判定；IOI 适合赛中多次提交累积得分；OI 适合赛中测公开样例、赛后统一评分。"),
            ("时长、开赛时间", "时长单位是分钟，留空表示不限时。开赛时间留空由选手点「开始比赛」启动；统一开赛可填 2026-12-05 09:00+08:00。图形界面也接受本机时区的时间。")])
    g.h("添加题目")
    g.p("点「添加题目」，填写唯一题号和题名，选择该题的题面 PDF 和数据 ZIP。需要英文显示时可另填英文题名，例如题名「两数之和」、英文显示题名 A+B。题号仍可用 A、B 区分。")
    g.p("在 OI 或 IOI 中，可另填源文件名，例如 sum。选手会在 sum/sum.cpp 中写代码。程序运行时是否读写 sum.in、sum.out，由数据包单独决定，见第 4 页。")
    g.p("选中一行可编辑、移除或用「上移」「下移」调整位置。移动后请检查题号和整册 PDF 的顺序；已有 PDF 的页序需要由组题人自行调整。")
    g.h("保留一份可迁移的工程")
    g.p("点「保存草稿」写入 JSON，下次可继续添加试题。「另存为…」可以为另一版比赛保留单独草稿。建议把 PDF、单题 ZIP 和草稿放在同一个项目目录下；迁移到另一台电脑时应保持目录结构。")
    g.note("从旧比赛出下一场", "先打开旧草稿，点「另存为…」保存新草稿。若要改某题数据，把新 ZIP 存成另一个文件名，再在新草稿中引用，以便两场比赛分别维护。")

    g.page("把测试数据导入单题", "已经有受支持的 QOJ 单题 ZIP 时，直接在「本题数据」中选择它。只有 .in 和答案文件时，在题目编辑窗口点「从数据目录制作…」。")
    g.code(["data/B/tests/1.in       data/B/tests/1.ans", "data/B/tests/2.in       data/B/tests/2.ans",
            "data/B/tests/3.in       data/B/tests/3.ans", "data/B/samples/1.in     data/B/samples/1.ans"],"以示例工程的 B 题为例")
    g.p("正式数据选 data/B/tests，公开样例选 data/B/samples。应直接选择存放文件的那一级目录。每个输入对应同名的 .ans 或 .out，只留一份答案。文件名中的数字按数值排序，如 2 在 10 前面。公开样例可以省略；要让 OI 选手赛中自测，应准备公开样例。")
    g.rows([("时间、内存、满分", "默认每点 1 秒、1024 MiB，题目满分 100。时限可使用小数，例如 0.25 秒。逐点等分时，4 个正式点中通过 3 个可得 75 分。"),
            ("计分方式", "「全部通过才得分」把整题作为一个计分单元，任一点未通过得 0 分。已有多组捆绑子任务时，使用带相应配置的受支持 QOJ ZIP。"),
            ("读写文件名", "留空使用标准输入输出。填 sum，则程序应读 sum.in、写 sum.out。填小写英文字母开头的标识，可含数字和下划线；不加扩展名。"),
            ("输出比较", "目录导入采用逐行文本比较。会忽略行尾普通空格及末尾空行；大小写、行内空格和中间空行都可能影响判定。")], 100)
    g.p("点「保存为 ZIP…」，把生成的包放在工程的 packages 目录。以后若修改原始 .in 或答案文件，请重新生成单题 ZIP，再导出整场比赛。")
    g.p("例如 B 的 1.in 可写 2 3，同目录的 1.ans 写 5。若输出包含文字，可用目录导入的文本比较；已有 ncmp 题包则按该整数检查器处理。")

    g.page("用两次提交验收赛包", "用实际导出的文件和一个单独的选手目录试跑。先确认正确的程序能通过，再故意做错一次，观察选手能看到哪些反馈。")
    g.code(['#include <iostream>', 'int main() { std::cout << "Hello World\\n"; }'],
           "选手目录中的 a.cpp · 对应 Hello World")
    g.code(["#include <iostream>", "int main() {", "    long long a, b;", "    std::cin >> a >> b;",
            "    std::cout << a + b << '\\n';", "}"],"选手目录中的 b.cpp · 对应 A+B")
    g.rows([("检查题面", "点「查看题册」打开两题整册，再按题号查看单题。B 的样例输入是 -7 4，样例输出应为 -3。"),
            ("正确提交", "在「提交评测…」同时勾选两题，A、B 都应得到 AC。选中 B 的记录，逐个查看测试点的状态、用时和内存。B 有一组输入是两个 2000000000，因此代码使用 64 位整数。"),
            ("错误提交", "把 b.cpp 的加号改成减号，仅提交 B。这次只应看到整题判定 WA。改回加号，保存并再次提交。选中新产生的 AC 记录即可查看各点明细。")], 83)
    g.note("分发与修订", "发给选手的是验收过的 .seexam。保留草稿、题面和原始数据用于修改。重新导出会生成新的比赛版本，适合另行验收和发放；同一轮训练请让所有选手使用同一个文件。")
    g.p("题册、单题 PDF、样例和正式数据在赛包中均加密保存。选手按开赛规则在本机获取题面和评测结果，无需教师口令。离线机器本身能够解密，应按可信练习环境使用。")

    g.page("选择合适的赛制", "赛制决定三个具体问题：什么时候读取选手代码，赛中能看到什么，以及最后怎样计分。下面三种流程都在选手机本地运行。")
    g.h("ICPC  ·  赛中判定")
    g.p("每次可勾选多题，使用正式数据评测。选中一条 AC 记录，可查看这次提交每个测试点的状态、用时和内存。未通过时只显示整题判定，如 WA 或 TLE。")
    g.h("OI  ·  赛后统一评测")
    g.p("赛中可自测公开样例，显示每点结果、用时和内存。定时比赛到期统一收取各题当前保存的源码并评测；不限时比赛由选手点「交卷」。未交或空代码的题得 0 分。")
    g.p("结束前应保存全部文件，并让 SelfEval 保持运行。若截止时软件已经退出，下次打开使用当时目录里实际保存的文件收卷。已经完成收卷的题目会使用已保存的快照。")
    g.h("IOI  ·  提交后即时计分")
    g.p("每次提交运行正式数据，显示逐点状态、本次得分和历史最佳。两个各占 50 分的独立子任务分别在不同提交中通过，合计可得 100 分。若题包把各点通过情况与附加扣分或整题取整关联，则按整题历史最高分结算。")
    g.note("开赛与重开", "自由练习从点击「开始比赛」计时。进行中退出再打开可继续原倒计时。自由 ICPC 练习到期后可以开始新一轮。设有固定开赛时间的比赛按原定时间开放和截止。")
    g.h("如何在彩排时验证计分")
    g.p("试建一场 IOI 比赛，设一道有两个独立正式点的题，每点 50 分。让第一份程序只过第 1 点，第二份程序只过第 2 点。每份提交都应显示 50 分，该题历史最佳应显示 100 分。再把计分方式换成「全部通过才得分」，另导一份新赛包，相同两次提交都应得 0 分。")

    g.page("选手怎样放代码、提交和恢复", "打开 .seexam 后选择自己的工作目录。所有程序需先在编辑器中保存好，再回到 SelfEval 勾选题目并提交。")
    g.h("ICPC 的文件布局")
    g.code(["my-work/", "  a.cpp", "  b.cpp"])
    g.p("以题号小写命名最方便。程序也可以按题名命名，或放进与题号、题名相同的子目录。例如 Hello World.cpp、A/A.cpp 都能对应 A。存在多份候选程序时优先查根目录的题号文件。")
    g.h("OI 和 IOI 的文件布局")
    g.code(["my-work/", "  problemset.pdf", "  sum/", "    sum.cpp     sum.pdf", "    1.in        1.ans"], "假设源文件名为 sum，且组题人提供了一个公开样例")
    g.p("开赛时软件会创建题目子目录，并补齐题面、代码文件和公开样例。按题面规定使用标准输入输出或文件读写，勿直接修改样例代替修改程序。再次进入同一目录会保留已经写好的代码。")
    g.h("一轮评测与下次启动")
    g.p("点「提交评测…」，勾选要测的题，等待评测完成。在「评测记录」中选中一次提交，可查看详情。内存以 MiB 表示，包含程序运行库实际驻留的页面。旧版保存的 ICPC AC 记录可能只有整题判定。要取得逐点明细，需在允许提交时重新评测并通过。")
    g.p("正常关闭后，从应用菜单重新打开会恢复上次的赛包、目录、题目和已有评测记录。需要改选目录时，主动点「打开考试…」并重新选择。赛包移动后也应重新指定文件。")

    g.page("按现象排查问题", "先确定错误出现在哪一步。组题端导出失败通常与材料有关；选手端提交后则应区分编译、答案判定和系统环境。")
    g.rows([("数据配对失败", "打开所选目录，逐项核对文件主名。若有 10.in，应配 10.ans 或 10.out；同名的两种答案同时出现会报错。正式和公开样例需分别选择。"),
            ("草稿引用失效", "重新选中移动过的整册 PDF、单题 PDF 或 ZIP，再保存草稿。已有 QOJ ZIP 需包含 problem.conf。交互、提交答案和未支持的自定义检查器无法按传统题导入。"),
            ("提示找不到代码", "先核对选手目录，再核对题号、子目录、扩展名和文件是否已保存。ICPC 先试根目录 a.cpp；OI / IOI 使用创建好的题目子目录。"),
            ("CE / WA / RE", "CE 先看编译器提示。WA 核对输入输出方式、格式及算法。RE 表示运行中异常退出，可用题面样例自行复现。TLE、MLE、OLE 分别表示超时、超内存和输出超限。"),
            ("评测环境错误", "在考试机执行下方依赖检查。若提示 bubblewrap 未能创建命名空间，交给考场管理员检查系统策略。可附上 Linux 版本、软件版本和错误全文。")], 99)
    g.h("命令行速查")
    g.code(["command -v g++ prlimit bwrap", "selfeval-studio build hello-sum/contest.json hello-sum.seexam",
            "mkdir -p my-work", "kamome-selfeval --cli --exam hello-sum.seexam --workspace my-work --start",
            "kamome-selfeval --cli --exam hello-sum.seexam A B"])
    g.p("上述命令使用 .deb 安装后的程序名。在便携目录中，可用 sh ./selfeval --studio build 和 sh ./selfeval --cli 运行对应命令。源码根目录运行 python3 release.py 可构建公开版源码 ZIP、便携包和 Debian 安装包。",size=10.2,leading=16.5)
    g.save()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path(__file__).with_name("manual-zh.pdf"))
    ap.add_argument("--regular", type=Path)
    ap.add_argument("--bold", type=Path)
    args = ap.parse_args()
    register_fonts(args.regular, args.bold)
    handbook(args.out)
