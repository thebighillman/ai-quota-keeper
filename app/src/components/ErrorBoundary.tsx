import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

type Props = {
  children: ReactNode;
  /** 页面标识: 换页面时用它做 key, 自动清掉上一页的错误状态 */
  pageKey?: string;
};

type State = {
  error: Error | null;
  stack: string;
  copied: boolean;
};

/**
 * 错误边界(V3.30.9): 单个页面渲染出错时只显示错误块, 不再整屏白。
 *
 * 为什么需要: 之前账号列表把数据包装对象当裸账号读, 一个 undefined 就让整个
 * React 树崩掉、页面全白, 用户看不出发生了什么, 也没法反馈。
 * 现在会把错误信息、组件栈留在界面上, 并提供"复制错误信息"给开发者。
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, stack: "", copied: false };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    const stack = String(info.componentStack || "").trim().slice(0, 1200);
    this.setState({ stack });
    // 控制台留一份, 方便开发时排查
    console.error("[页面出错]", error, info.componentStack);
  }

  private reset = () => {
    this.setState({ error: null, stack: "", copied: false });
  };

  private copy = async () => {
    const { error, stack } = this.state;
    const text = [
      `页面: ${this.props.pageKey || "未知"}`,
      `时间: ${new Date().toLocaleString("zh-CN")}`,
      `错误: ${error?.name}: ${error?.message}`,
      `组件栈:`,
      stack,
    ].join("\n");
    try {
      await navigator.clipboard.writeText(text);
      this.setState({ copied: true });
      window.setTimeout(() => this.setState({ copied: false }), 2500);
    } catch {
      // 剪贴板不可用(非 https 等): 退化成选中文本
      window.prompt("复制下面的错误信息发给开发者:", text);
    }
  };

  render() {
    const { error, stack, copied } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="bg-white rounded-xl p-6 shadow-sm border border-red-200">
        <div className="flex items-start gap-3">
          <span className="text-2xl leading-none">⚠️</span>
          <div className="min-w-0 flex-1">
            <div className="font-semibold text-red-700">这个页面出错了，但软件没坏</div>
            <div className="text-[13px] text-slate-600 mt-1 leading-relaxed">
              其他页面照常可用（点左侧导航切走即可）。这通常是我这边代码的 bug，
              把下面的信息发我就能定位。
            </div>

            <div className="mt-3 bg-red-50 border border-red-100 rounded-lg px-3 py-2">
              <div className="text-[12px] font-mono text-red-800 break-all">
                {error.name}: {error.message}
              </div>
            </div>

            {stack && (
              <details className="mt-2">
                <summary className="text-[12px] text-slate-500 cursor-pointer select-none">
                  展开技术细节（组件位置）
                </summary>
                <pre className="mt-2 text-[11px] text-slate-500 bg-slate-50 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap max-h-56">
{stack}
                </pre>
              </details>
            )}

            <div className="flex flex-wrap gap-2 mt-4">
              <button
                onClick={this.copy}
                className="px-3 py-1.5 text-[13px] rounded-lg bg-[#1B6B7A] text-white hover:bg-[#155a66]"
              >
                {copied ? "已复制 ✓" : "复制错误信息"}
              </button>
              <button
                onClick={this.reset}
                className="px-3 py-1.5 text-[13px] rounded-lg border border-slate-300 text-slate-700 hover:bg-slate-50"
              >
                重试这个页面
              </button>
              <button
                onClick={() => window.location.reload()}
                className="px-3 py-1.5 text-[13px] rounded-lg border border-slate-300 text-slate-700 hover:bg-slate-50"
              >
                重新加载整个界面
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }
}
