import modal
import subprocess
app = modal.App("direct-script-runner")

# 1. Define the environment AND attach the local script directly to the image
image = (
    modal.Image.debian_slim()
    .pip_install("requests", "beautifulsoup4")
    .add_local_file("vote_cat_resilient.py", remote_path="/root/vote_cat_resilient.py")
)

# 3. Define the cloud function
@app.function(image=image)
def run_machine(machine_id):
    print(f"Booting cloud machine {machine_id}...")
    
    # This runs your script exactly as you would type it in your own terminal
    subprocess.run([
        "python", "/root/vote_cat_resilient.py", 
        "--hearts", "1200", 
        "--workers", "4"
    ])

# 4. Trigger the parallel execution
@app.local_entrypoint()
def main():
    print("Launching 100 cloud machines...")
    
    # .map() spins up 100 containers and runs the subprocess on all of them
    list(run_machine.map(range(1, 201)))