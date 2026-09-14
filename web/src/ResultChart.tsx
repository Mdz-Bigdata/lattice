// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef } from "react";
import { init, use } from "echarts/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  AriaComponent,
  LegendComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { DataGrid, Empty } from "./components";
import type { QueryResult } from "./api";

use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  AriaComponent,
  LegendComponent,
  CanvasRenderer,
]);
export type ChartMode = "line" | "bar" | "horizontal" | "pie" | "table";

export default function ResultChart({
  result,
  mode,
}: {
  result: QueryResult;
  mode: ChartMode;
}) {
  const chartRef = useRef<HTMLDivElement>(null);
  const dimensionIndex = result.columns.indexOf(result.chart?.dimension);
  const metricIndex = result.columns.indexOf(result.chart?.metric);
  const hasChart = dimensionIndex >= 0 && metricIndex >= 0;
  useEffect(() => {
    if (
      !chartRef.current ||
      mode === "table" ||
      !hasChart ||
      !result.rows.length
    )
      return;
    const chart = init(chartRef.current);
    const labels = result.rows.map((row) =>
      String(row[dimensionIndex] ?? "空值"),
    );
    const values = result.rows.map((row) => Number(row[metricIndex]) || 0);
    const horizontal = mode === "horizontal";
    const categoryAxis = {
      type: "category" as const,
      data: labels,
      axisTick: { show: false },
      axisLine: { lineStyle: { color: "#e3e6eb" } },
      axisLabel: { color: "#7c8491", fontSize: 12, hideOverlap: true },
    };
    const valueAxis = {
      type: "value" as const,
      axisLabel: { color: "#7c8491", fontSize: 12 },
      splitLine: { lineStyle: { type: "dashed" as const, color: "#e8ebef" } },
    };
    const axisOptions =
      mode === "pie"
        ? {}
        : {
            xAxis: horizontal ? valueAxis : categoryAxis,
            yAxis: horizontal ? categoryAxis : valueAxis,
          };
    chart.setOption({
      color: ["#4387f5", "#65b3cf", "#69c5a7", "#8e9acb", "#e7b870", "#a0be72"],
      animationDuration: 350,
      aria: {
        enabled: true,
        label: {
          description: `${result.title}，共 ${result.rows.length} 条结果。切换到数据表查看完整数据。`,
        },
      },
      textStyle: { fontFamily: 'Arial, "PingFang SC", sans-serif' },
      grid: {
        top: 30,
        right: 30,
        bottom: 34,
        left: horizontal ? 80 : 90,
        containLabel: true,
      },
      tooltip: {
        trigger: mode === "pie" ? "item" : "axis",
        confine: true,
        renderMode: "richText",
        backgroundColor: "#fff",
        borderColor: "#eff0f3",
        textStyle: { fontSize: 13, color: "#454b56" },
        padding: 16,
      },
      ...axisOptions,
      legend: mode === "pie" ? { bottom: 0, type: "scroll" } : undefined,
      series: [
        {
          name: result.chart.metric,
          type: mode === "horizontal" ? "bar" : mode,
          ...(mode === "pie"
            ? {
                radius: ["0%", "67%"],
                center: ["50%", "46%"],
                data: labels.map((name, i) => ({ name, value: values[i] })),
                label: { formatter: "{b}: {d}%" },
              }
            : {
                data: values,
                barMaxWidth: 33,
                itemStyle: {
                  borderRadius: horizontal ? [0, 3, 3, 0] : [3, 3, 0, 0],
                },
                ...(mode === "line"
                  ? {
                      smooth: false,
                      symbol: "circle",
                      symbolSize: 6,
                      lineStyle: { width: 2 },
                      areaStyle: { opacity: 0.04 },
                    }
                  : {}),
              }),
        },
      ],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(chartRef.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [result, mode, dimensionIndex, metricIndex, hasChart]);
  if (mode === "table" || !hasChart)
    return <DataGrid columns={result.columns} rows={result.rows} />;
  if (!result.rows.length)
    return <Empty title="查询完成，暂无数据">请调整查询条件。</Empty>;
  return (
    <div
      className="result-chart"
      ref={chartRef}
      role="img"
      aria-label={`${result.title} ${mode} 图表`}
    />
  );
}
