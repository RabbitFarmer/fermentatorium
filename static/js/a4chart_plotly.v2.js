document.addEventListener("DOMContentLoaded", () => {
    const chartDiv = document.getElementById("chart");
    const limitSelect = document.getElementById("limit");
    const reloadButton = document.getElementById("reload");
    const statusSpan = document.getElementById("status");

    const fetchAndRenderChart = async () => {
        const limit = limitSelect.value;

        try {
            statusSpan.textContent = "Loading…";

            const response = await fetch(`/chart_data/${tiltColor}?limit=${limit}`);

            if (!response.ok) {
                throw new Error(`Error loading data: ${response.statusText}`);
            }

            const data = await response.json();

            const { timestamps, gravities, temps } = data;

            const gravityTrace = {
                x: timestamps,
                y: gravities,
                name: "Gravity (SG)",
                type: "scatter",
                mode: "lines+markers",
                line: { color: "blue" },
            };

            const tempTrace = {
                x: timestamps,
                y: temps,
                name: "Temperature (°F)",
                type: "scatter",
                mode: "lines+markers",
                line: { color: "red" },
            };

            Plotly.newPlot(chartDiv, [gravityTrace, tempTrace], {
                title: `Fermentation Chart for ${brewName}`,
                xaxis: { title: "Time" },
                yaxis: { title: "Values" },
            });

            statusSpan.textContent = "Loaded.";
        } catch (err) {
            console.error(err);
            statusSpan.textContent = "Failed to load data.";
        }
    };

    reloadButton.addEventListener("click", fetchAndRenderChart);
    fetchAndRenderChart();
});
