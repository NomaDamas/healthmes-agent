using HealthMes.Glance.Core;
using HealthMes.Windows.Common;

namespace HealthMes.Tray;

/// <summary>
/// 24-hour energy curve, honest about nulls: segments are only drawn between
/// ADJACENT hours that both carry data; isolated hours become dots; missing
/// hours stay blank (never interpolated — mirrors the widget guidance in
/// docs/design/WATCH-NOTIFICATIONS.ko.md Q5, which owns the final missing-
/// data visual; this rendering is placeholder plumbing).
/// </summary>
internal sealed class SparklineControl : Control
{
    private IReadOnlyList<GlanceCurvePoint> _points = [];

    public SparklineControl()
    {
        DoubleBuffered = true;
        AccessibleName = L10n.Get("Ax_Sparkline");
        AccessibleRole = AccessibleRole.Graphic;
        TabStop = false;
    }

    public void SetCurve(IReadOnlyList<GlanceCurvePoint> points)
    {
        _points = points;
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        var graphics = e.Graphics;
        graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;

        // Placeholder palette: single accent, no severity colors (Q2 pending).
        using var linePen = new Pen(SystemColors.Highlight, 2f);
        using var dotBrush = new SolidBrush(SystemColors.Highlight);
        using var axisPen = new Pen(SystemColors.ControlDark, 1f);

        var rect = ClientRectangle;
        if (rect.Width < 24 || rect.Height < 10 || _points.Count == 0)
        {
            return;
        }
        graphics.DrawLine(axisPen, rect.Left, rect.Bottom - 1, rect.Right, rect.Bottom - 1);

        PointF? Point(int index)
        {
            var score = _points[index].Score;
            if (score is null)
            {
                return null;
            }
            var x = rect.Left + (rect.Width - 1) * (index / (float)Math.Max(1, _points.Count - 1));
            var y = rect.Bottom - 2 - (rect.Height - 4) * (Math.Clamp(score.Value, 0, 100) / 100f);
            return new PointF(x, y);
        }

        for (var i = 0; i < _points.Count; i++)
        {
            var current = Point(i);
            if (current is null)
            {
                continue;
            }
            var next = i + 1 < _points.Count ? Point(i + 1) : null;
            var previous = i > 0 ? Point(i - 1) : null;
            if (next is not null)
            {
                graphics.DrawLine(linePen, current.Value, next.Value);
            }
            if (next is null && previous is null)
            {
                graphics.FillEllipse(dotBrush, current.Value.X - 2f, current.Value.Y - 2f, 4f, 4f);
            }
        }
    }
}
