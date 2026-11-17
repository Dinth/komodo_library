<?php
/*
 * Grocy product picker: build the product -> barcodes lookup once, instead of
 * a linear search through all barcode rows for every product.
 *
 * Run at container start by the entrypoint in compose.yaml (see the comment
 * there for the measurements and for when to remove it). Reported upstream as
 * https://github.com/grocy/grocy/issues/3022.
 *
 * Usage: php productpicker-barcode-map.php <path to productpicker.blade.php>
 * Only touches the file when it is byte-identical to the Grocy 4.7.1 template
 * and the result is byte-identical to the tested patched template; otherwise
 * it changes nothing and exits 1.
 */

$file = $argv[1] ?? '';
$originalSha256 = '7b59d32d58dd2cba6386032afd5c96b4cda6a9b379063a98d7234f8f5a7355cf';
$patchedSha256 = 'acebac9cce8574d5d23aca76cdce202781ac2bf6a1e31b231a6f9ee7784accac';

$old = <<<'OLD'
		@foreach($products as $product)
		@php $bc = null;
		if(isset($barcodes)) {
		$bc = FindObjectInArrayByPropertyValue($barcodes, 'product_id', $product->id);
		}
		@endphp
		<option data-additional-searchdata="@if(isset($bc)){{ strtolower($bc->barcodes) }}@endif,"
OLD;

$new = <<<'NEW'
		@php $barcodesByProductId = [];
		if(isset($barcodes)) {
		foreach($barcodes as $barcodeRow) {
		if(!array_key_exists($barcodeRow->product_id, $barcodesByProductId)) {
		$barcodesByProductId[$barcodeRow->product_id] = $barcodeRow->barcodes;
		}
		}
		}
		@endphp
		@foreach($products as $product)
		<option data-additional-searchdata="@if(isset($barcodesByProductId[$product->id])){{ strtolower($barcodesByProductId[$product->id]) }}@endif,"
NEW;

$source = @file_get_contents($file);
if ($source === false || hash('sha256', $source) !== $originalSha256) {
	fwrite(STDERR, "productpicker patch: $file is not the Grocy 4.7.1 template\n");
	exit(1);
}

$patched = str_replace($old, $new, $source, $count);
if ($count !== 1 || hash('sha256', $patched) !== $patchedSha256) {
	fwrite(STDERR, "productpicker patch: the replacement did not produce the expected template\n");
	exit(1);
}

exit(file_put_contents($file, $patched) === strlen($patched) ? 0 : 1);
