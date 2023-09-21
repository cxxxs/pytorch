
#include <torch/csrc/inductor/aoti_torch/c/shim.h>
#include <torch/csrc/inductor/aoti_torch/tensor_converter.h>

namespace torch {
namespace aot_inductor {

at::Tensor* tensor_handle_to_tensor_pointer(AtenTensorHandle handle) {
  return reinterpret_cast<at::Tensor*>(handle);
}

AtenTensorHandle tensor_pointer_to_tensor_handle(at::Tensor* tensor) {
  return reinterpret_cast<AtenTensorHandle>(tensor);
}

std::vector<AtenTensorHandle> unsafe_alloc_new_handles_from_tensors(
    at::Tensor* tensors,
    size_t length) {
  std::vector<AtenTensorHandle> result(length);
  for (size_t i = 0; i < length; i++) {
    auto allocated = new at::Tensor(std::move(tensors[i]));
    result[i] = tensor_pointer_to_tensor_handle(allocated);
  }
  return result;
}

std::vector<at::Tensor> alloc_tensors_by_stealing_from_handles(
    AtenTensorHandle* handles,
    size_t length) {
  std::vector<at::Tensor> result;
  result.reserve(length);
  for (size_t i = 0; i < length; i++) {
    result.emplace_back(
        std::move(*tensor_handle_to_tensor_pointer(handles[i])));
    handles[i] = nullptr;
  }
  return result;
}

} // namespace aot_inductor
} // namespace torch
