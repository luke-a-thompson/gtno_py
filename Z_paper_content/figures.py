import numpy as np
import numpy.typing as npt
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from pathlib import Path
from usyd_colors import get_palette

grey, red, blue, yellow, white = get_palette("primary").hex_colors()


def set_matplotlib_style(font_size: int = 14) -> None:
    """
    Set the matplotlib style to use Times New Roman font with size 17.

    Returns:
        None
    """
    font_path = "/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf"
    fm.fontManager.addfont(font_path)

    plt.rcParams.update(
        {
            "font.family": "Times New Roman",  # Directly specify the font family
            "font.size": font_size,
            "mathtext.fontset": "stix",  # Use STIX fonts for math expressions
            "axes.titlesize": font_size,
            "axes.labelsize": font_size,
            "xtick.labelsize": font_size,
            "ytick.labelsize": font_size,
            "legend.fontsize": font_size,
        }
    )


def plot_lambda_value_residuals(weights_dir: Path, figure_file_name: str, figure_dir: Path = Path("Z_paper_content/lambda_value_residuals")) -> None:
    """
    Plot the lambda values as a line chart showing their evolution over time.

    Args:
        weights_dir: Directory containing the weights file
        figure_file_name: Name of the figure file
        figure_dir: Directory to save the figure as PDF

    Returns:
        None
    """
    # Load the lambda values
    weights = np.load(weights_dir / "lambda_v_residual.npz", allow_pickle=True)
    lambda_values = weights["lambda_v_residual"]

    print(f"Lambda values shape: {lambda_values.shape}")

    # Create a figure
    fig, ax = plt.subplots(figsize=(12, 6))

    # Get the number of timesteps and lambda values
    n_timesteps: int = lambda_values.shape[0]
    n_lambda_values: int = lambda_values.shape[1]

    # Create 10 evenly spaced indices for every 100 timesteps
    selected_indices: list[int] = []
    for i in range(10):
        selected_indices.append(i * (n_timesteps // 10))
    if n_timesteps - 1 not in selected_indices:
        selected_indices.append(n_timesteps - 1)  # Always include the last element

    # Create x-axis values (timesteps)
    x: npt.NDArray[np.int32] = np.arange(n_timesteps)

    # Define colors for the lambda values
    colors: list[str] = [red, blue, yellow, grey, "purple"]

    # Plot each lambda value as a separate line
    for i in range(n_lambda_values):
        ax.plot(x, lambda_values[:, i], label=f"λ{i+1}", color=colors[i % len(colors)], linewidth=2)

    # Add vertical lines at selected timesteps
    for idx in selected_indices:
        ax.axvline(x=idx, color="gray", linestyle="--", alpha=0.5)

    # Remove top and right spines (borders)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # Add title and labels
    ax.set_xlabel("Training Step")
    ax.set_ylabel("Lambda Values")

    # Add legend
    ax.legend(loc="best")

    # Ensure the directory exists
    figure_dir.mkdir(parents=True, exist_ok=True)

    # Create the save path
    save_path = figure_dir / f"lambda_values_{Path(figure_file_name).stem}.pdf"

    # Save as PDF with high quality
    plt.savefig(save_path, format="pdf", dpi=300, bbox_inches="tight")
    print(f"Figure saved as PDF to {save_path}")

    plt.show()


def plot_learnable_attention_weights(
    weights_dir: Path,
    figure_file_name: str,
    data_file_name: str = "attention_denom.npz",
    figure_dir: Path = Path("Z_paper_content/attention_weights"),
    step_size: int = 50,
) -> None:
    """
    Plot the learnable attention weights for each layer as a time-series box plot.

    Args:
        weights_dir: Directory containing the weights file
        figure_file_name: Name of the figure file
        data_file_name: Name of the data file containing weights
        figure_dir: Directory to save the figure as PDF
        step_size: Plot every step_size-th element (default: 25)

    Returns:
        None
    """

    weights: npt.NDArray[np.float32] = np.load(weights_dir / data_file_name, allow_pickle=True)
    attention_denom: npt.NDArray[np.float32] = weights["attention_denom"]
    print(f"Attention denom shape: {attention_denom.shape}")

    # Create a figure
    fig, ax = plt.subplots(figsize=(12, 6))

    # Generate positions for the boxes
    n_timesteps: int = attention_denom.shape[0]
    # Flatten the last two dimensions (layers and heads)
    # Original shape: [timesteps, layers, heads]
    # New shape: [timesteps, layers*heads]
    n_layers: int = attention_denom.shape[1]
    n_heads: int = attention_denom.shape[2]
    attention_denom = attention_denom.reshape(n_timesteps, n_layers * n_heads)
    print(f"Reshaped attention denom: {attention_denom.shape} (timesteps, layers*heads)")

    # Select indices at regular intervals based on step_size
    selected_indices: list[int] = list(range(0, n_timesteps, step_size))
    if n_timesteps - 1 not in selected_indices:
        selected_indices.append(n_timesteps - 1)  # Always include the last element

    positions: npt.NDArray[np.int32] = np.array(selected_indices) + 1

    # Create the box plot directly from the numpy array, but only for selected timesteps
    box_plot = ax.boxplot(
        [attention_denom[t] for t in selected_indices],
        positions=positions,
        patch_artist=True,
        widths=step_size * 0.75,
        showfliers=False,  # Hide outliers
    )

    # Customize the box plot appearance
    for box in box_plot["boxes"]:
        box.set(facecolor=blue, alpha=0.8)

    for median in box_plot["medians"]:
        median.set_color(red)  # Set median line color
        median.set_linewidth(2)  # Optional: Adjust line width for better visibility

    # Remove top and right spines (borders)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # Add title and labels
    ax.set_xlabel("Training Step")
    ax.set_ylabel("Attention Denominator")

    # Set the x-tick positions and labels
    ax.set_xticks(positions)
    # Only add 1 to the first and last indices
    x_labels = []
    for i, idx in enumerate(selected_indices):
        if i == 0 or i == len(selected_indices) - 1:  # First or last element
            x_labels.append(str(idx + 1))
        else:
            x_labels.append(str(idx))
    ax.set_xticklabels(x_labels)

    plt.tight_layout()
    plt.ylim(15, 17)

    # Save the figure
    # Ensure the directory exists
    figure_dir.mkdir(parents=True, exist_ok=True)

    # Create the save path with the filename derived from the weights file
    save_path = figure_dir / f"attention_weights_{Path(figure_file_name).stem}.pdf"

    # Save as PDF with high quality
    plt.savefig(save_path, format="pdf", dpi=300, bbox_inches="tight")
    print(f"Figure saved as PDF to {save_path}")

    plt.show()


if __name__ == "__main__":
    set_matplotlib_style()
    # plot_learnable_attention_weights(Path("benchmark_runs/Paper_learned_denom_ethanol_06-Mar-2025_01-36-47/weights_run1"), "ethanol")
    plot_lambda_value_residuals(Path("benchmark_runs/Paper_learned_denom_toluene_06-Mar-2025_02-29-46/weights_run1"), "ethanol")
