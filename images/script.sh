#!/bin/bash

# Remove specific png files: 1 2 3 7 8 9 14 15
# Then rename remaining numbered png files to 1.png ... n.png

shopt -s nullglob

# Files to delete
to_delete=(1 2 3 7 8 9 14 15)

for n in "${to_delete[@]}"; do
    [ -f "$n.png" ] && rm -- "$n.png"
done

# Get remaining numbered png files sorted numerically
files=([0-9]*.png)

if [ ${#files[@]} -eq 0 ]; then
    echo "All files removed."
    exit 0
fi

mapfile -t sorted_files < <(
printf "%s\n" "${files[@]}" | sort -V
)

# Temporary rename
counter=1
for file in "${sorted_files[@]}"; do
    mv -- "$file" "__tmp_$counter.png"
    ((counter++))
done

# Final rename
counter=1
for file in $(ls __tmp_*.png | sort -V); do
    mv -- "$file" "$counter.png"
    ((counter++))
done

echo "Done. Deleted selected files and reordered remaining files from 1.png to $((counter-1))."

